-- Benchmark-specific schema subset: object/array/string/integer, required,
-- additionalProperties, minimum and maxItems. It covers the entire fixture
-- contract; it is deliberately not advertised as a general JSON Schema engine.
local json = require('cjson.safe')
if json.decode_array_with_array_mt then json.decode_array_with_array_mt(true) end
local pgmoon = require('pgmoon')
local pkey = require('resty.openssl.pkey')
local semaphore = require('ngx.semaphore')
local websocket = require('resty.websocket.server')
local M = {}
local function read(path) local f=assert(io.open(path));local s=f:read('*a');f:close();return s end
local manifest = assert(json.decode(read('/bench/contract.json')))
local key = assert(pkey.new(read('/bench/jwt-public.pem')))
local endpoints = {}
for _, e in ipairs(manifest.endpoints) do endpoints[e.name]=e end
local pool_size = tonumber(os.getenv('POOL_SIZE') or '4')
local slots = assert(semaphore.new(pool_size))
local db_config = assert(json.decode(read('/bench/reference.json'))).database
local clients, socket_count, listener_ready = {}, 0, false

local function valid(schema,value)
    if schema.type=='object' then
        if type(value)~='table' or (json.array_mt and getmetatable(value)==json.array_mt) then return false end
        for _,name in ipairs(schema.required or {}) do if value[name]==nil then return false end end
        for name,item in pairs(value) do
            if schema.properties and schema.properties[name] then
                if not valid(schema.properties[name],item) then return false end
            elseif schema.additionalProperties==false then return false end
        end
    elseif schema.type=='array' then
        if type(value)~='table' or (schema.maxItems and #value>schema.maxItems) then return false end
        for _,item in ipairs(value) do if not valid(schema.items,item) then return false end end
    elseif schema.type=='integer' then
        if type(value)~='number' or value%1~=0 or (schema.minimum and value<schema.minimum) then return false end
    elseif schema.type=='string' then
        if type(value)~='string' then return false end
    else return false end
    return true
end

local function decode64(s)
    s=s:gsub('-','+'):gsub('_','/')
    return ngx.decode_base64(s .. string.rep('=', (4-#s%4)%4))
end
local function auth(token)
    if type(token)~='string' or #token>8192 then return nil end
    local h,p,s=token:match('^([^.]+)%.([^.]+)%.([^.]+)$')
    if not h then return nil end
    local header=json.decode(decode64(h) or '')
    local claims=json.decode(decode64(p) or '')
    if not header or header.alg~='RS256' or header.kid~='pgxbench' or not claims then return nil end
    local sig=decode64(s)
    if not sig or not key:verify(sig,h..'.'..p,'sha256') then return nil end
    local audience=claims.aud=='pgxbench'
    if type(claims.aud)=='table' then for _,v in ipairs(claims.aud) do if v=='pgxbench' then audience=true end end end
    if not audience or claims.iss~='pgxbench' or type(claims.exp)~='number' or claims.exp%1~=0 or claims.exp<=ngx.time()
       or type(claims.sub)~='string' or #claims.sub==0 or #claims.sub>256 or claims.sub:find('\0',1,true) then return nil end
    return claims
end
local function database(listener)
    local config={pool_size=pool_size,backlog=0,pool_name=listener and 'listener' or 'queries'}
    for name,value in pairs(db_config) do config[name]=value end
    local pg=pgmoon.new(config)
    pg:settimeout(6000)
    return pg
end
local function transaction(claims,name,input,output,readonly,queued)
    local ok=slots:wait(queued and 60 or 0)
    if not ok then return nil end
    local pg=database(false)
    local success,value=pcall(function()
        assert(pg:connect())
        assert(pg:query(readonly and 'BEGIN READ ONLY' or 'BEGIN'))
        assert(pg:query([[SELECT jsonb_build_object('claims',set_config('request.jwt.claims',$1,true),
            'timeout',set_config('statement_timeout','4000',true),'lock',set_config('lock_timeout','1000',true),
            'path',set_config('search_path','pg_catalog,web',true))]],json.encode({sub=claims.sub,role='web'})))
        local rows=assert(pg:query('SELECT web.'..name..'($1::jsonb) AS value',json.encode(input)))
        local result=rows[1].value
        local encoded=assert(json.encode(result))
        assert(#encoded<=1048576 and (not output or valid(output,result)) and claims.exp>ngx.time())
        assert(pg:query('COMMIT'))
        assert(pg:keepalive(0,pool_size))
        return result
    end)
    if not success then pcall(function() pg:disconnect() end) end
    slots:post(1)
    return success and value or nil
end

function M.start()
    ngx.timer.at(0,function(premature)
        if premature then return end
        local backoff=1
        while not ngx.worker.exiting() do
            local pg=database(true)
            local connected,connect_error=pg:connect()
            if not connected then ngx.log(ngx.ERR,'benchmark LISTEN connect: ',connect_error or 'unknown') end
            if connected and pg:query('LISTEN pgxio_events') then
                listener_ready=true;backoff=1
                pg:settimeout(1000)
                while not ngx.worker.exiting() do
                    local notification,err=pg:wait_for_notification()
                    if notification then
                        for state in pairs(clients) do
                            for _,sub in pairs(state.subs) do
                                if sub.key==notification.payload then
                                    sub.dirty=true
                                    if state.signal:count()<1 then state.signal:post(1) end
                                end
                            end
                        end
                    elseif not err or not err:find('timeout',1,true) then
                        ngx.log(ngx.ERR,'benchmark LISTEN lost: ',err or 'unknown');break
                    end
                end
            end
            listener_ready=false
            for state in pairs(clients) do state.closing=true end
            pcall(function() pg:disconnect() end)
            ngx.sleep(backoff);backoff=math.min(backoff*2,30)
        end
    end)
end

local function ws_handle()
    if not listener_ready or socket_count>=tonumber(os.getenv('MAX_WEBSOCKETS') or '1024') then return ngx.exit(503) end
    local protocol=ngx.req.get_headers()['sec-websocket-protocol'] or ''
    if not protocol:find('pgxio.v1',1,true) then return ngx.exit(400) end
    ngx.header['Sec-WebSocket-Protocol']='pgxio.v1'
    local ws=websocket:new({timeout=1000,max_payload_len=16384})
    if not ws then return end
    local state={ws=ws,subs={},closing=false,created=ngx.time(),last_ping=ngx.time(),signal=assert(semaphore.new(0))}
    local sendlock=assert(semaphore.new(1))
    local function send(value)
        if not sendlock:wait(10) then state.closing=true;return end
        local ok=ws:send_text(json.encode(value));sendlock:post(1)
        if not ok then state.closing=true end
    end
    clients[state]=true;socket_count=socket_count+1
    local authorization=ngx.thread.spawn(function()
        while not state.closing do
            state.signal:wait(1)
            for id,sub in pairs(state.subs) do
                if (sub.initial or sub.dirty) and not sub.pending then
                    sub.pending=true;sub.dirty=false
                    -- One coroutine per socket shares the bounded worker pool.
                    local route=transaction(state.claims,'bench_authorize',{},nil,true,true)
                    if type(route)~='string' or not listener_ready or (sub.key and sub.key~=route) then
                        state.closing=true;break
                    end
                    sub.key=route
                    send({type=sub.initial and 'subscribed' or 'changed',id=id})
                    sub.initial=false;sub.pending=false
                end
            end
        end
    end)
    while not state.closing and not ngx.worker.exiting() do
        if (state.claims and state.claims.exp<=ngx.time()) or (not state.claims and ngx.time()-state.created>10) then break end
        local data,kind,err=ws:recv_frame()
        if kind=='close' or (not data and err and not err:find('timeout',1,true)) then break end
        if kind=='ping' then
            if sendlock:wait(10) then ws:send_pong(data);sendlock:post(1) end
        elseif kind=='text' then
            local message=json.decode(data)
            if not message then break end
            if not state.claims then
                state.claims=message.type=='auth' and auth(message.token) or nil
                if not state.claims then break end
                send({type='ready'})
            elseif message.type=='subscribe' then
                local count=0;for _ in pairs(state.subs) do count=count+1 end
                if type(message.id)~='string' or #message.id==0 or #message.id>64 or state.subs[message.id]
                    or message.stream~='bench' or type(message.params)~='table' or next(message.params)
                    or count>=16 then break end
                state.subs[message.id]={initial=true,pending=false,dirty=false}
                if state.signal:count()<1 then state.signal:post(1) end
            elseif message.type=='unsubscribe' then
                if state.subs[message.id] and state.subs[message.id].pending then break end
                state.subs[message.id]=nil
            else break end
        end
        if ngx.time()-state.last_ping>=30 and sendlock:wait(10) then
            ws:send_ping('');sendlock:post(1);state.last_ping=ngx.time()
        end
    end
    state.closing=true
    -- Let an active transaction return its pool slot before ending the request.
    ngx.thread.wait(authorization)
    pcall(function() ws:send_close() end)
    clients[state]=nil;socket_count=socket_count-1
end

function M.handle()
    if ngx.var.uri=='/readyz' then
        ngx.status=(listener_ready and slots:count()>0) and 200 or 503
        ngx.header.content_type='application/json';ngx.say('{"status":"ready"}');return
    end
    if ngx.var.uri=='/v1/events' then return ws_handle() end
    local name=ngx.var.uri:match('^/v1/([a-z_]+)$')
    local endpoint=name and endpoints[name]
    if ngx.req.get_method()~='POST' or not endpoint then return ngx.exit(404) end
    local bearer=(ngx.req.get_headers().authorization or ''):match('^Bearer (.+)$')
    local claims=auth(bearer)
    if not claims then return ngx.exit(401) end
    ngx.req.read_body()
    local input=json.decode(ngx.req.get_body_data() or '')
    if not input or not valid(endpoint.input,input) then return ngx.exit(400) end
    local result=transaction(claims,name,input,endpoint.output,endpoint.read_only,false)
    if not result then return ngx.exit(503) end
    ngx.header.content_type='application/json';ngx.say(json.encode(result))
end
return M
