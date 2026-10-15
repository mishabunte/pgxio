// A benchmark reference. No general routing, ORM, replay, or automatic write retries.
package main

import (
 "context"
 "crypto"
 "crypto/rsa"
 "crypto/sha256"
 "crypto/tls"
 "crypto/x509"
 "encoding/base64"
 "encoding/json"
 "encoding/pem"
 "errors"
 "fmt"
 "io"
 "log"
 "net/http"
 "os"
 "strconv"
 "strings"
 "sync"
 "sync/atomic"
 "time"

 "github.com/coder/websocket"
 "github.com/jackc/pgx/v5"
 "github.com/jackc/pgx/v5/pgxpool"
 "github.com/santhosh-tekuri/jsonschema/v5"
)

type endpoint struct {
 Name string `json:"name"`
 ReadOnly bool `json:"read_only"`
 Input json.RawMessage `json:"input"`
 Output json.RawMessage `json:"output"`
 input, output *jsonschema.Schema
}
type claims struct { Sub string `json:"sub"`; Exp int64 `json:"exp"`; Iss string `json:"iss"`; Aud any `json:"aud"` }
type subscription struct { key string; pending, dirty bool }
type client struct {
 ws *websocket.Conn
 ctx context.Context
 cancel context.CancelFunc
 mu sync.Mutex
 writeMu sync.Mutex
 claims *claims
 subs map[string]*subscription
 created time.Time
}
type server struct {
 pool *pgxpool.Pool
 slots chan struct{}
 endpoints map[string]*endpoint
 key *rsa.PublicKey
 ready atomic.Bool
 mu sync.RWMutex
 clients map[*client]bool
 maxWS int
}

func envInt(name string, fallback int) int { n, e := strconv.Atoi(os.Getenv(name)); if e != nil || n < 1 { return fallback }; return n }
func mustRead(path string) []byte { b,e:=os.ReadFile(path); if e!=nil {panic(e)}; return b }
func compile(raw json.RawMessage, name string) *jsonschema.Schema {
 c:=jsonschema.NewCompiler()
 if e:=c.AddResource(name, strings.NewReader(string(raw))); e!=nil {panic(e)}
 result,e:=c.Compile(name); if e!=nil {panic(e)}; return result
}
func (s *server) authenticate(token string) (*claims,error) {
 bad:=errors.New("unauthorized")
 if len(token)>8192 {return nil,bad}
 parts:=strings.Split(token,"."); if len(parts)!=3 {return nil,bad}
 header,e:=base64.RawURLEncoding.DecodeString(parts[0]); if e!=nil {return nil,bad}
 var h struct { Alg string `json:"alg"`; Kid string `json:"kid"` }
 if json.Unmarshal(header,&h)!=nil || h.Alg!="RS256" || h.Kid!="pgxbench" {return nil,bad}
 sig,e:=base64.RawURLEncoding.DecodeString(parts[2]); if e!=nil {return nil,bad}
 hash:=sha256.Sum256([]byte(parts[0]+"."+parts[1]))
 if rsa.VerifyPKCS1v15(s.key,crypto.SHA256,hash[:],sig)!=nil {return nil,bad}
 payload,e:=base64.RawURLEncoding.DecodeString(parts[1]); if e!=nil {return nil,bad}
 var c claims
 if json.Unmarshal(payload,&c)!=nil || c.Exp<=time.Now().Unix() || c.Iss!="pgxbench" || len(c.Sub)==0 || len(c.Sub)>256 || strings.ContainsRune(c.Sub,0) {return nil,bad}
 audience:=c.Aud=="pgxbench"
 if values,ok:=c.Aud.([]any); ok {for _,v:=range values {if v=="pgxbench" {audience=true}}}
 if !audience {return nil,bad}; return &c,nil
}

func (s *server) query(ctx context.Context, c *claims, name string, input any, check *jsonschema.Schema, readOnly, wait bool) (any,error) {
 if wait { select { case s.slots<-struct{}{}: case <-ctx.Done():return nil,ctx.Err() } } else {
  select { case s.slots<-struct{}{}: default:return nil,errors.New("busy") }
 }
 defer func(){<-s.slots}()
 ctx,cancel:=context.WithTimeout(ctx,6*time.Second); defer cancel()
 conn,e:=s.pool.Acquire(ctx); if e!=nil {return nil,e}; defer conn.Release()
 // On any failure, close the connection to abandon a possibly open transaction.
 ok:=false; defer func(){if !ok {_=conn.Conn().Close(context.Background())}}()
 begin:="BEGIN"; if readOnly {begin="BEGIN READ ONLY"}
 if _,e=conn.Exec(ctx,begin); e!=nil {return nil,e}
 claimJSON,_:=json.Marshal(map[string]string{"sub":c.Sub,"role":"web"})
 _,e=conn.Exec(ctx,`SELECT jsonb_build_object('claims',set_config('request.jwt.claims',$1,true),
 'timeout',set_config('statement_timeout','4000',true),'lock',set_config('lock_timeout','1000',true),
 'path',set_config('search_path','pg_catalog,web',true))`,string(claimJSON))
 if e!=nil {return nil,e}
 body,_:=json.Marshal(input)
 var raw []byte
 if e=conn.QueryRow(ctx,"SELECT web."+name+"($1::jsonb)",string(body)).Scan(&raw); e!=nil {return nil,e}
 if len(raw)>1048576 {return nil,errors.New("large result")}
 var value any
 if json.Unmarshal(raw,&value)!=nil || (check!=nil && check.Validate(value)!=nil) || c.Exp<=time.Now().Unix() {return nil,errors.New("invalid result")}
 if _,e=conn.Exec(ctx,"COMMIT"); e!=nil {return nil,e}; ok=true
 return value,nil
}
func reply(w http.ResponseWriter,status int,body any) {w.Header().Set("Content-Type","application/json"); w.WriteHeader(status); _=json.NewEncoder(w).Encode(body)}
func (s *server) ServeHTTP(w http.ResponseWriter,r *http.Request) {
 if r.URL.Path=="/readyz" {if s.ready.Load() && len(s.slots)<cap(s.slots) {reply(w,200,map[string]string{"status":"ready"})} else {reply(w,503,map[string]string{"status":"unavailable"})};return}
 if r.URL.Path=="/v1/events" {s.websocket(w,r);return}
 e:=s.endpoints[strings.TrimPrefix(r.URL.Path,"/v1/")]
 if r.Method!="POST" || e==nil {reply(w,404,map[string]string{"error":"not_found"});return}
 authorization:=r.Header.Get("Authorization")
 if !strings.HasPrefix(authorization,"Bearer ") {reply(w,401,map[string]string{"error":"unauthorized"});return}
 c,err:=s.authenticate(strings.TrimPrefix(authorization,"Bearer "))
 if err!=nil {reply(w,401,map[string]string{"error":"unauthorized"});return}
 raw,err:=io.ReadAll(http.MaxBytesReader(w,r.Body,65536))
 var input any
 if err!=nil || json.Unmarshal(raw,&input)!=nil || e.input.Validate(input)!=nil {reply(w,400,map[string]string{"error":"invalid_input"});return}
 value,err:=s.query(r.Context(),c,e.Name,input,e.output,e.ReadOnly,false)
 if err!=nil {reply(w,503,map[string]string{"error":"database_error"});return}; reply(w,200,value)
}
func (c *client) send(value any) error {
 c.writeMu.Lock(); defer c.writeMu.Unlock()
 ctx,cancel:=context.WithTimeout(c.ctx,10*time.Second);defer cancel()
 data,_:=json.Marshal(value);return c.ws.Write(ctx,websocket.MessageText,data)
}
func (c *client) close() {c.cancel(); _=c.ws.CloseNow()}
func (s *server) authorize(c *client,id string,sub *subscription,initial bool) {
 c.mu.Lock()
 if sub.pending {c.mu.Unlock();return}; sub.pending=true;sub.dirty=false
 c.mu.Unlock()
 go func(){
  value,err:=s.query(c.ctx,c.claims,"bench_authorize",map[string]any{},nil,true,true)
  routing,valid:=value.(string)
  c.mu.Lock()
  denied:=err!=nil || !valid || routing=="" || !s.ready.Load() || (sub.key!="" && sub.key!=routing)
  if !denied {sub.key=routing}
  c.mu.Unlock()
  if denied {c.close()} else {
   kind:="changed"; if initial {kind="subscribed"}
   if c.send(map[string]string{"type":kind,"id":id})!=nil {c.close()}
  }
  c.mu.Lock();sub.pending=false;dirty:=sub.dirty;c.mu.Unlock()
  if dirty && c.ctx.Err()==nil {s.authorize(c,id,sub,false)}
 }()
}
func (s *server) websocket(w http.ResponseWriter,r *http.Request) {
 if !s.ready.Load() {reply(w,503,map[string]string{"error":"unavailable"});return}
 s.mu.Lock()
 if len(s.clients)>=s.maxWS {s.mu.Unlock();reply(w,503,map[string]string{"error":"limit"});return}
 ws,err:=websocket.Accept(w,r,&websocket.AcceptOptions{Subprotocols:[]string{"pgxio.v1"}})
 if err!=nil {s.mu.Unlock();return}
 if ws.Subprotocol()!="pgxio.v1" {s.mu.Unlock();_ = ws.CloseNow();return}
 ws.SetReadLimit(16384)
 ctx,cancel:=context.WithCancel(context.Background())
 c:=&client{ws:ws,ctx:ctx,cancel:cancel,subs:map[string]*subscription{},created:time.Now()}
 s.clients[c]=true;s.mu.Unlock()
 defer func(){c.close();s.mu.Lock();delete(s.clients,c);s.mu.Unlock()}()
 go func(){
  ticker:=time.NewTicker(time.Second);defer ticker.Stop();ticks:=0
  for {select {
   case <-ctx.Done():return
   case <-ticker.C:
    c.mu.Lock();expired:=(c.claims==nil && time.Since(c.created)>10*time.Second)||(c.claims!=nil && c.claims.Exp<=time.Now().Unix());c.mu.Unlock()
    if expired {c.close();return};ticks++
    if ticks%30==0 {pingCtx,stop:=context.WithTimeout(ctx,10*time.Second);err:=ws.Ping(pingCtx);stop();if err!=nil {c.close();return}}
  }}
 }()
 for {
  _,data,err:=ws.Read(ctx);if err!=nil {return}
  var message struct {Type string `json:"type"`;Token string `json:"token"`;ID string `json:"id"`;Stream string `json:"stream"`;Params map[string]any `json:"params"`}
  if json.Unmarshal(data,&message)!=nil {return}
  c.mu.Lock();authenticated:=c.claims!=nil;c.mu.Unlock()
  if !authenticated {
   if message.Type!="auth" {return};claims,err:=s.authenticate(message.Token);if err!=nil {return}
   c.mu.Lock();c.claims=claims;c.mu.Unlock()
   if c.send(map[string]string{"type":"ready"})!=nil {return};continue
  }
  if message.Type=="subscribe" {
   c.mu.Lock()
   if len(message.ID)==0||len(message.ID)>64||message.Stream!="bench"||len(message.Params)!=0||c.subs[message.ID]!=nil||len(c.subs)>=16 {c.mu.Unlock();return}
   sub:=&subscription{};c.subs[message.ID]=sub;c.mu.Unlock();s.authorize(c,message.ID,sub,true)
  } else if message.Type=="unsubscribe" {
   c.mu.Lock();sub:=c.subs[message.ID];if sub!=nil&&sub.pending {c.mu.Unlock();return};delete(c.subs,message.ID);c.mu.Unlock()
  } else {return}
 }
}
func (s *server) listen(url string) {
 backoff:=time.Second
 for {
  ctx:=context.Background();conn,err:=pgx.Connect(ctx,url)
  if err==nil {_,err=conn.Exec(ctx,"LISTEN pgxio_events")}
  if err==nil {
   s.ready.Store(true);backoff=time.Second
   for {
    notification,e:=conn.WaitForNotification(ctx);if e!=nil {break}
    s.mu.RLock()
    for client:=range s.clients {
     client.mu.Lock();list:=map[string]*subscription{}
     for id,sub:=range client.subs {if sub.key==notification.Payload {sub.dirty=true;list[id]=sub}}
     client.mu.Unlock();for id,sub:=range list {s.authorize(client,id,sub,false)}
    }
    s.mu.RUnlock()
   }
  }
  s.ready.Store(false);if conn!=nil {_=conn.Close(ctx)}
  s.mu.RLock();for client:=range s.clients {client.close()};s.mu.RUnlock()
  time.Sleep(backoff);if backoff<30*time.Second {backoff*=2;if backoff>30*time.Second {backoff=30*time.Second}}
 }
}
func main() {
 var manifest struct {Endpoints []*endpoint `json:"endpoints"`}
 if err:=json.Unmarshal(mustRead("/bench/contract.json"),&manifest);err!=nil {panic(err)}
 block,_:=pem.Decode(mustRead("/bench/jwt-public.pem"));if block==nil {panic("missing key")}
 key,err:=x509.ParsePKIXPublicKey(block.Bytes);if err!=nil {panic(err)}
 config,err:=pgxpool.ParseConfig(os.Getenv("DATABASE_URL"));if err!=nil {panic(err)}
 config.MaxConns=int32(envInt("POOL_SIZE",4));config.MinConns=config.MaxConns
 config.ConnConfig.ConnectTimeout=6*time.Second
 pool,err:=pgxpool.NewWithConfig(context.Background(),config);if err!=nil {panic(err)}
 s:=&server{pool:pool,slots:make(chan struct{},config.MaxConns),endpoints:map[string]*endpoint{},key:key.(*rsa.PublicKey),clients:map[*client]bool{},maxWS:envInt("MAX_WEBSOCKETS",1024)}
 for _,e:=range manifest.Endpoints {e.input=compile(e.Input,e.Name+"-input.json");e.output=compile(e.Output,e.Name+"-output.json");s.endpoints[e.Name]=e}
 go s.listen(os.Getenv("DATABASE_URL"))
 if os.Getenv("FRONTEND_TLS")=="0" {
  log.Fatal((&http.Server{Addr:":8080",Handler:s,ReadHeaderTimeout:10*time.Second}).ListenAndServe())
 }
 go func(){log.Fatal((&http.Server{Addr:":8080",Handler:s,ReadHeaderTimeout:10*time.Second}).ListenAndServe())}()
 fmt.Println("pgxbench Go reference listening")
 log.Fatal((&http.Server{Addr:":8443",Handler:s,ReadHeaderTimeout:10*time.Second,
  TLSConfig:&tls.Config{MinVersion:tls.VersionTLS13,MaxVersion:tls.VersionTLS13}}).ListenAndServeTLS("/bench/tls-cert.pem","/bench/tls-key.pem"))
}
