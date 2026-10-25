//! pgxio alpha: single-threaded application state inside each Nginx worker.
//! Callbacks never finalize requests while holding WORKER: finalization invokes
//! request cleanup synchronously. Borrowed input is detached before pool cleanup.
mod auth;
mod buffer;
#[cfg(feature = "borrowed-libpq")]
mod borrowed;
mod config;
mod contract;
mod db;
mod ffi;
mod input;
mod output;
mod ws;

use auth::{Auth, Claims};
use buffer::{BufferMode, PlaintextPool};
use config::Config;
use contract::Contract;
use db::{Connection, Event, Job};
use ffi::Request;
use ngx::ffi::{ngx_http_module_t, ngx_module_t, NGX_HTTP_MODULE};
use serde_json::{json, Value};
use std::{
    cell::RefCell,
    collections::{HashMap, VecDeque},
    ffi::CString,
    io::ErrorKind,
    sync::Arc,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tungstenite::Message;

ngx::ngx_modules!(ngx_http_pgxio_module);
static CTX: ngx_http_module_t = ngx_http_module_t {
    preconfiguration: None,
    postconfiguration: None,
    create_main_conf: Some(ffi::pgx_create_conf),
    init_main_conf: Some(ffi::pgx_init_conf),
    create_srv_conf: None,
    merge_srv_conf: None,
    create_loc_conf: None,
    merge_loc_conf: None,
};
#[used]
#[no_mangle]
pub static mut ngx_http_pgxio_module: ngx_module_t = ngx_module_t {
    ctx: &CTX as *const _ as _,
    commands: &raw mut ffi::pgx_commands,
    type_: NGX_HTTP_MODULE,
    init_process: Some(ffi::pgx_init_process),
    exit_process: Some(ffi::pgx_exit_process),
    ..ngx_module_t::default()
};

thread_local! { static WORKER:RefCell<Option<Worker>>=const { RefCell::new(None) }; }
struct Pending {
    request: Request,
    endpoint: usize,
    claims: Claims,
}
struct Waiting {
    job: Job,
    deadline: Option<Instant>,
}
struct Worker {
    plaintext_pool: PlaintextPool,
    #[cfg(feature = "benchmark-auth")]
    buffer_stats: [u64; 3],
    config: Config,
    contract: Contract,
    auth: Auth,
    url: CString,
    input: Vec<Option<Arc<jsonschema::Validator>>>,
    output: Vec<Option<Arc<jsonschema::Validator>>>,
    stream_input: Vec<Option<Arc<jsonschema::Validator>>>,
    db: Vec<Connection>,
    http: HashMap<u64, Pending>,
    ws: HashMap<u64, ws::Client>,
    queue: VecDeque<Waiting>,
    ready_ws: VecDeque<(u64, String)>,
    next: u64,
}
enum Action {
    Reply(Request, i32, Value),
    RawReply(Request, Vec<u8>),
    Close(Request),
    Arm(Request, bool, bool),
}
fn run(f: impl FnOnce(&mut Worker) -> Vec<Action>) {
    let actions = WORKER.with(|cell| {
        let mut state = cell.borrow_mut();
        state
            .as_mut()
            .map(|worker| {
                let actions = f(worker);
                worker.arm_deadline();
                actions
            })
            .unwrap_or_default()
    });
    let mut finalized = std::collections::HashSet::new();
    for a in actions {
        unsafe {
            match a {
                Action::Reply(r, s, v) => {
                    if finalized.insert(r) {
                        let body = v.to_string();
                        ffi::pgx_reply(r, s, body.as_ptr(), body.len());
                    }
                }
                Action::RawReply(r, body) => {
                    if finalized.insert(r) {
                        ffi::pgx_reply(r, 200, body.as_ptr(), body.len());
                    }
                }
                Action::Close(r) => {
                    if finalized.insert(r) {
                        ffi::pgx_close(r);
                    }
                }
                Action::Arm(r, w, a) => {
                    if !finalized.contains(&r) {
                        ffi::pgx_ws_arm(r, w.into(), a.into());
                    }
                }
            }
        }
    }
}
pub fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
fn error(code: &str) -> Value {
    json!({"error":{"code":code}})
}

#[repr(C)]
pub struct ConfigString {
    len: usize,
    data: *const u8,
}
#[repr(C)]
pub struct ConfigDirective {
    name: ConfigString,
    count: usize,
    args: *const ConfigString,
}
unsafe fn config_text(s: &ConfigString) -> Result<String, String> {
    if s.len == 0 {
        return Ok(String::new());
    }
    std::str::from_utf8(std::slice::from_raw_parts(s.data, s.len))
        .map(str::to_owned)
        .map_err(|_| "configuration must be UTF-8".into())
}
#[no_mangle]
pub unsafe extern "C" fn pgx_config_new(
    raw: *const ConfigDirective,
    count: usize,
) -> *mut config::Settings {
    let parse = || -> Result<config::Settings, String> {
        let mut directives = Vec::new();
        for d in std::slice::from_raw_parts(raw, count) {
            let args = std::slice::from_raw_parts(d.args, d.count)
                .iter()
                .map(|s| config_text(s))
                .collect::<Result<Vec<_>, _>>()?;
            directives.push((config_text(&d.name)?, args));
        }
        config::Settings::parse(&directives)
    };
    match parse() {
        Ok(settings) => Box::into_raw(Box::new(settings)),
        Err(e) => {
            eprintln!("pgxio configuration: {e}");
            std::ptr::null_mut()
        }
    }
}
#[no_mangle]
pub unsafe extern "C" fn pgx_config_free(settings: *mut config::Settings) {
    if !settings.is_null() {
        drop(Box::from_raw(settings));
    }
}
#[no_mangle]
pub unsafe extern "C" fn pgx_init(settings: *const config::Settings) -> i32 {
    let init = || -> Result<Worker, String> {
        let config::Settings {
            config,
            routes: contract,
        } = (*settings).clone();
        let mut auth = Auth::new(
            config.issuer.clone(),
            config.audience.clone(),
            config.jwks_file.as_deref(),
            config.development,
        )?;
        auth.cache_limits(config.jwt_cache_max_entries, config.jwt_cache_max_bytes);
        if unsafe { pq_sys::PQlibVersion() } < 170000 {
            return Err("libpq 17 or later is required".into());
        }
        let url = db::validate_url(
            &std::env::var("PGXIO_DATABASE_URL").map_err(|_| "missing PGXIO_DATABASE_URL")?,
            config.development,
        )?;
        let compile = |v: &Value| {
            jsonschema::validator_for(v)
                .map(Arc::new)
                .map_err(|e| e.to_string())
        };
        let input = contract
            .endpoints
            .iter()
            .map(|e| {
                if config.validate_rest_input {
                    compile(&e.input).map(Some)
                } else {
                    Ok(None)
                }
            })
            .collect::<Result<_, _>>()?;
        let output = contract
            .endpoints
            .iter()
            .map(|e| {
                if config.validate_output {
                    compile(&e.output).map(Some)
                } else {
                    Ok(None)
                }
            })
            .collect::<Result<_, _>>()?;
        let stream_input = contract
            .streams
            .iter()
            .map(|e| {
                if config.validate_rest_input {
                    compile(&e.input).map(Some)
                } else {
                    Ok(None)
                }
            })
            .collect::<Result<_, _>>()?;
        let db = (0..=config.pool_size)
            .map(|id| {
                Connection::new(
                    id,
                    id == config.pool_size,
                    config.db_prepared_statements,
                    config.db_pipeline,
                    config.libpq_buf,
                )
            })
            .collect();
        Ok(Worker {
            plaintext_pool: PlaintextPool::default(),
            #[cfg(feature = "benchmark-auth")]
            buffer_stats: [0; 3],
            config,
            contract,
            auth,
            url,
            input,
            output,
            stream_input,
            db,
            http: HashMap::new(),
            ws: HashMap::new(),
            queue: VecDeque::new(),
            ready_ws: VecDeque::new(),
            next: 1,
        })
    };
    match init() {
        Ok(worker) => {
            WORKER.with(|w| *w.borrow_mut() = Some(worker));
            pgx_tick();
            0
        }
        Err(e) => {
            eprintln!("pgxio configuration: {e}");
            -1
        }
    }
}
#[no_mangle]
pub extern "C" fn pgx_exit() {
    // Nginx may exit as soon as only cancelable timers remain. Explicitly close
    // held requests before dropping Rust state; OS process exit is not cleanup.
    run(|w| {
        // PQfinish must stop using request buffers before Nginx frees pools.
        for connection in &mut w.db {
            connection.disconnect();
        }
        w.ws.values()
            .map(|c| Action::Close(c.request))
            .chain(w.http.values().map(|p| Action::Close(p.request)))
            .collect()
    });
    WORKER.with(|w| {
        w.borrow_mut().take();
    });
}

#[no_mangle]
pub unsafe extern "C" fn pgx_start(r: Request) -> isize {
    let mut n = 0;
    let p = ffi::pgx_uri(r, &mut n);
    let uri = String::from_utf8_lossy(std::slice::from_raw_parts(p, n)).into_owned();
    let p = ffi::pgx_method(r, &mut n);
    let method = String::from_utf8_lossy(std::slice::from_raw_parts(p, n)).into_owned();
    // An explicit reference keeps immediate and asynchronous replies on the same
    // cleanup path; NGX_DONE releases the core handler reference.
    ffi::pgx_hold(r);
    let mut body = false;
    let mut single_body = true;
    run(|w| {
        let origin = ffi::header(r, "origin");
        if !origin.is_empty() {
            if !w.config.origins.contains(&origin) {
                return vec![Action::Reply(r, 403, error("origin_denied"))];
            }
            ffi::add_header(r, c"Access-Control-Allow-Origin", &origin);
            ffi::add_header(r, c"Vary", "Origin");
        }
        if method == "OPTIONS" {
            ffi::add_header(r, c"Access-Control-Allow-Methods", "GET, POST, OPTIONS");
            ffi::add_header(
                r,
                c"Access-Control-Allow-Headers",
                "Authorization, Content-Type",
            );
            return vec![Action::Reply(r, 204, Value::Null)];
        }
        match (method.as_str(), uri.as_str()) {
            #[cfg(feature = "benchmark-auth")]
            ("GET", "/__pgxio_buffers") => return vec![Action::Reply(r, 200, json!({
                "copy": w.buffer_stats[0], "borrowed": w.buffer_stats[1],
                "auto_copied": w.buffer_stats[2],
                "plaintext": w.plaintext_pool.all_plaintext(w.config.pool_size)
            }))],
            ("GET", "/healthz") => return vec![Action::Reply(r, 200, json!({"status":"ok"}))],
            ("GET", "/readyz") => {
                return vec![Action::Reply(
                    r,
                    if w.db.iter().any(Connection::usable) && w.listener_ready() {
                        200
                    } else {
                        503
                    },
                    json!({"listener":w.listener_ready()}),
                )]
            }
            ("GET", "/openapi.json") => return vec![Action::Reply(r, 200, w.contract.openapi())],
            ("GET", "/v1/events") => return w.upgrade(r),
            _ => {}
        }
        if method != "POST" {
            return vec![Action::Reply(r, 405, error("method_not_allowed"))];
        }
        let Some(i) = w
            .contract
            .endpoints
            .iter()
            .position(|e| uri == format!("/v1/{}", e.name))
        else {
            return vec![Action::Reply(r, 404, error("not_found"))];
        };
        let auth = ffi::header(r, "authorization");
        let endpoint = &w.contract.endpoints[i];
        let authenticated = if w
            .config
            .benchmark_bypass(&endpoint.name, &endpoint.function)
        {
            Ok(Claims {
                sub: "benchmark-anonymous".into(),
                exp: u64::MAX,
                iss: w.config.issuer.clone(),
                aud: json!(w.config.audience),
            })
        } else {
            auth.strip_prefix("Bearer ")
                .ok_or(())
                .and_then(|s| w.auth.verify(s))
        };
        let Ok(claims) = authenticated else {
            return vec![Action::Reply(r, 401, error("unauthorized"))];
        };
        if !ffi::header(r, "content-type")
            .split(';')
            .next()
            .is_some_and(|s| s.trim().eq_ignore_ascii_case("application/json"))
        {
            return vec![Action::Reply(r, 415, error("json_required"))];
        }
        let id = w.next;
        w.next += 1;
        if ffi::pgx_attach(r, id) != 0 {
            return vec![Action::Reply(r, 500, error("allocation_failed"))];
        }
        w.http.insert(
            id,
            Pending {
                request: r,
                endpoint: i,
                claims,
            },
        );
        single_body = w.input_mode() == BufferMode::Copy;
        body = true;
        vec![]
    });
    if body {
        // read_client_request_body owns another reference. Release our explicit
        // hold first; the body callback may immediately complete the request.
        release_hold(r);
        let rc = ffi::pgx_read_body(r, single_body.into());
        if rc >= 300 {
            return rc;
        }
    }
    -4 // NGX_DONE
}
extern "C" {
    fn pgx_release_hold(r: Request);
}
unsafe fn release_hold(r: Request) {
    pgx_release_hold(r);
}

unsafe fn request_input(
    r: Request,
    client: u64,
    mode: BufferMode,
) -> Result<input::Input, input::Error> {
    let length = ffi::pgx_body_length(r);
    if length == -2 || length > input::MAX_BODY as isize {
        return Err(input::Error::TooLarge);
    }
    if length < 0 {
        return Err(input::Error::InvalidJson);
    }
    if mode != BufferMode::Copy {
        let count = ffi::pgx_body_segments(r, std::ptr::null_mut(), 0);
        if count < 0 {
            return Err(input::Error::InvalidJson);
        }
        if mode.select(length as usize, count as usize, cfg!(feature = "borrowed-libpq"), false, true) {
            let mut parts = Vec::with_capacity(count as usize);
            if ffi::pgx_body_segments(r, parts.as_mut_ptr(), count as usize) != count {
                return Err(input::Error::InvalidJson);
            }
            parts.set_len(count as usize);
            return input::Input::borrow(client, parts);
        }
    }
    input::copy_body(length, |dst, cap| ffi::pgx_copy_body(r, dst, cap)).map(Into::into)
}

#[no_mangle]
pub unsafe extern "C" fn pgx_body(r: Request) {
    ffi::pgx_wait_client(r);
    let id = ffi::pgx_id(r);

    run(|w| {
        let Some(p) = w.http.get(&id) else {
            return vec![];
        };
        let input = request_input(
            r,
            id,
            w.input_mode(),
        )
        .and_then(|bytes| {
            if let Some(schema) = &w.input[p.endpoint] {
                input::check_schema(bytes.owned(), schema)?;
            }
            Ok(bytes)
        });
        let input = match input {
            Ok(input) => {
                #[cfg(feature = "benchmark-auth")]
                { w.buffer_stats[usize::from(input.borrowed())] += 1; }
                input
            },
            Err(input::Error::TooLarge) => {
                return vec![Action::Reply(r, 413, error("body_too_large"))]
            }
            Err(input::Error::InvalidJson) => {
                return vec![Action::Reply(r, 400, error("invalid_json"))]
            }
            Err(input::Error::InvalidInput) => {
                return vec![Action::Reply(r, 400, error("invalid_input"))]
            }
        };
        let e = &w.contract.endpoints[p.endpoint];
        let job = Job {
            client: id,
            subscription: None,
            function: e.function.clone(),
            input: Some(input),
            subject: p.claims.sub.clone(),
            expires: p.claims.exp,
            read_only: e.read_only,
            output: w.output[p.endpoint].clone(),
        };
        if w.queue.is_empty() {
            if let Some(conn) = w.db.iter().position(Connection::idle) {
                let events = w.db[conn].start(job);
                return w.events(events);
            }
        }
        if w.config.pool_acquisition_timeout_ms == 0 || w.queue.len() >= w.config.pool_queue_limit {
            return vec![Action::Reply(r, 503, error("busy"))];
        }
        w.queue.push_back(Waiting {
            job,
            deadline: Some(
                Instant::now() + Duration::from_millis(w.config.pool_acquisition_timeout_ms),
            ),
        });
        w.schedule()
    });
}
#[no_mangle]
pub extern "C" fn pgx_cleanup(id: u64) {
    run(|w| {
        w.http.remove(&id);
        w.ws.remove(&id);
        w.queue.retain(|j| j.job.client != id);
        w.ready_ws.retain(|(client, _)| *client != id);
        let mut events = vec![];
        for c in &mut w.db {
            if c.references(id) {
                events.extend(c.cancel());
            }
        }
        let mut actions = w.events(events);
        actions.extend(w.schedule());
        actions
    });
}
#[no_mangle]
pub extern "C" fn pgx_database(id: usize) {
    run(|w| {
        let events = w.db[id].advance();
        let mut a = w.events(events);
        a.extend(w.schedule());
        a
    });
}
#[no_mangle]
pub extern "C" fn pgx_tick() {
    run(|w| {
        let mut events = vec![];
        for c in &mut w.db {
            events.extend(c.tick(&w.url));
        }
        let mut a = w.events(events);
        w.auth.prune();
        let stopping = unsafe { ffi::pgx_stopping() != 0 };
        for c in w.ws.values_mut() {
            let expired = c
                .claims
                .as_ref()
                .map_or(c.created.elapsed() > Duration::from_secs(10), |claims| {
                    claims.exp <= now()
                });
            let pong_late = c
                .pong_due
                .is_some_and(|t| t.elapsed() > Duration::from_secs(10));
            if expired || stopping || pong_late {
                c.closing = true;
            }
            if !c.closing && c.last_ping.elapsed() > Duration::from_secs(30) {
                c.last_ping = Instant::now();
                c.pong_due = Some(Instant::now());
                if !c.write(Message::Ping(vec![].into())) {
                    c.closing = true;
                }
            }
            if c.closing {
                a.push(Action::Close(c.request));
            } else {
                a.push(Action::Arm(c.request, c.needs_write, false));
            }
        }
        a.extend(w.schedule());
        a
    });
}

impl Worker {
    fn input_mode(&self) -> BufferMode {
        if self.config.libpq_buf.may_borrow(cfg!(feature = "borrowed-libpq"),
            self.config.validate_rest_input, self.plaintext_pool.all_plaintext(self.config.pool_size)) {
            self.config.libpq_buf
        } else {
            BufferMode::Copy
        }
    }
    fn listener_ready(&self) -> bool {
        self.db.last().is_some_and(Connection::listening)
    }
    fn events(&mut self, events: Vec<Event>) -> Vec<Action> {
        let mut actions = vec![];
        for event in events {
            match event {
                Event::Transport(id, plain) => self.plaintext_pool.update(id, plain),
                Event::AutoCopied => {
                    #[cfg(feature = "benchmark-auth")]
                    { self.buffer_stats[2] += 1; }
                },
                Event::Done(job, result) => {
                    if let Some(id) = job.subscription {
                        let Some(c) = self.ws.get_mut(&job.client) else {
                            continue;
                        };
                        let Some(s) = c.subscriptions.get_mut(&id) else {
                            continue;
                        };
                        s.pending = false;
                        if s.dirty && !s.ready {
                            s.ready = true;
                            self.ready_ws.push_back((job.client, id.clone()));
                        }
                        let key = result
                            .ok()
                            .and_then(|v| match v {
                                db::Output::Internal(v) => v.as_str().map(str::to_owned),
                                _ => None,
                            })
                            .filter(|s| !s.is_empty() && s.len() <= 128);
                        if key.is_none()
                            || s.key.as_ref().is_some_and(|old| Some(old) != key.as_ref())
                            || !self.db.last().unwrap().listening()
                        {
                            c.closing = true;
                            actions.push(Action::Close(c.request));
                            continue;
                        }
                        let first = s.key.is_none();
                        s.key = key;
                        if !c.send(json!({"type":if first {"subscribed"}else{"changed"},"id":id})) {
                            c.closing = true;
                            actions.push(Action::Close(c.request));
                        } else {
                            actions.push(Action::Arm(c.request, c.needs_write, false));
                        }
                    } else if let Some(p) = self.http.get(&job.client) {
                        actions.push(match result {
                            Ok(db::Output::Http(bytes)) => Action::RawReply(p.request, bytes),
                            Err(db::Error::InvalidInput) => {
                                Action::Reply(p.request, 400, error("invalid_input"))
                            }
                            _ => Action::Reply(p.request, 503, error("database_error")),
                        });
                    }
                }
                Event::Notify(key) => {
                    for (&client, c) in &mut self.ws {
                        for (id, s) in &mut c.subscriptions {
                            if s.key.as_deref() == Some(&key) {
                                s.dirty = true;
                                if !s.pending && !s.ready {
                                    s.ready = true;
                                    self.ready_ws.push_back((client, id.clone()));
                                }
                            }
                        }
                    }
                }
                Event::ListenerLost => {
                    for c in self.ws.values_mut() {
                        if !c.closing {
                            c.closing = true;
                            actions.push(Action::Close(c.request));
                        }
                    }
                }
            }
        }
        actions
    }
    fn ws_job(&mut self) -> Option<Job> {
        while let Some((client, id)) = self.ready_ws.pop_front() {
            let Some(c) = self.ws.get_mut(&client) else {
                continue;
            };
            let Some(s) = c.subscriptions.get_mut(&id) else {
                continue;
            };
            s.ready = false;
            if c.closing || s.pending || !s.dirty {
                continue;
            }
            let Some(claims) = &c.claims else {
                continue;
            };
            s.dirty = false;
            s.pending = true;
            return Some(Job {
                client,
                subscription: Some(id),
                function: self.contract.streams[s.stream].authorize.clone(),
                input: Some(CString::new(s.params.to_string()).unwrap().into()),
                subject: claims.sub.clone(),
                expires: claims.exp,
                read_only: true,
                output: None,
            });
        }
        None
    }
    fn schedule(&mut self) -> Vec<Action> {
        let mut actions = vec![];
        let now = Instant::now();
        let wall = crate::now();
        let waiting = std::mem::take(&mut self.queue);
        for item in waiting {
            if item.job.expires <= wall {
                if item.job.subscription.is_some() {
                    actions
                        .extend(self.events(vec![Event::Done(item.job, Err(db::Error::Database))]));
                } else if let Some(p) = self.http.get(&item.job.client) {
                    actions.push(Action::Reply(p.request, 401, error("unauthorized")));
                }
            } else if item.deadline.is_some_and(|deadline| deadline <= now) {
                if let Some(p) = self.http.get(&item.job.client) {
                    actions.push(Action::Reply(p.request, 503, error("pool_timeout")));
                }
            } else {
                self.queue.push_back(item);
            }
        }
        let mut events = vec![];
        for index in 0..self.db.len() {
            if self.db[index].idle() {
                let job = if let Some(item) = self.queue.pop_front() {
                    Some(item.job)
                } else {
                    self.ws_job()
                };
                if let Some(job) = job {
                    if job.expires <= wall {
                        events.push(Event::Done(job, Err(db::Error::Database)));
                    } else {
                        events.extend(self.db[index].start(job));
                    }
                }
            }
        }
        // Only bounded jobs hold cloned JSON. Deferred subscriptions retain a
        // coalesced marker and rotate in FIFO order rather than being rejected.
        while self.queue.len() < self.config.pool_queue_limit {
            let Some(job) = self.ws_job() else {
                break;
            };
            self.queue.push_back(Waiting {
                job,
                deadline: None,
            });
        }
        actions.extend(self.events(events));
        actions
    }
    fn arm_deadline(&self) {
        let next = self
            .db
            .iter()
            .filter_map(Connection::next_deadline)
            .chain(self.queue.iter().filter_map(|item| item.deadline))
            .min();
        let delay = next.map_or(1000, |at| {
            at.saturating_duration_since(Instant::now())
                .as_millis()
                .clamp(1, 1000) as u64
        });
        unsafe {
            ffi::pgx_schedule_tick(delay);
        }
    }
    unsafe fn upgrade(&mut self, r: Request) -> Vec<Action> {
        if self.ws.len() >= self.config.max_websockets || !self.listener_ready() {
            return vec![Action::Reply(r, 503, error("unavailable"))];
        }
        let upgrade = ffi::header(r, "upgrade");
        let connection = ffi::header(r, "connection");
        let key = ffi::header(r, "sec-websocket-key");
        let protocol = ffi::header(r, "sec-websocket-protocol");
        if ffi::pgx_can_upgrade(r) == 0
            || ffi::pgx_has_args(r) != 0
            || !upgrade.eq_ignore_ascii_case("websocket")
            || !connection
                .split(',')
                .any(|s| s.trim().eq_ignore_ascii_case("upgrade"))
            || ffi::header(r, "sec-websocket-version") != "13"
            || !protocol.split(',').any(|s| s.trim() == "pgxio.v1")
            || key.len() != 24
            || !key.ends_with("==")
            || !key.as_bytes()[..22]
                .iter()
                .all(|c| c.is_ascii_alphanumeric() || *c == b'+' || *c == b'/')
            || !matches!(key.as_bytes()[21], b'A' | b'Q' | b'g' | b'w')
        {
            return vec![Action::Reply(r, 400, error("invalid_handshake"))];
        }
        // A valid Sec-WebSocket-Key encodes exactly 16 bytes in canonical base64.
        let Ok(req) = tungstenite::http::Request::builder()
            .method("GET")
            .uri("/v1/events")
            .header("Host", "pgxio")
            .header("Connection", "Upgrade")
            .header("Upgrade", "websocket")
            .header("Sec-WebSocket-Version", "13")
            .header("Sec-WebSocket-Key", &key)
            .body(())
        else {
            return vec![Action::Reply(r, 400, error("invalid_handshake"))];
        };
        let Ok(response) = tungstenite::handshake::server::create_response(&req) else {
            return vec![Action::Reply(r, 400, error("invalid_handshake"))];
        };
        ffi::add_header(r, c"Upgrade", "websocket");
        ffi::add_header(
            r,
            c"Sec-WebSocket-Accept",
            response.headers()["sec-websocket-accept"].to_str().unwrap(),
        );
        ffi::add_header(r, c"Sec-WebSocket-Protocol", "pgxio.v1");
        let id = self.next;
        self.next += 1;
        if ffi::pgx_attach(r, id) != 0 {
            return vec![Action::Reply(r, 500, error("allocation_failed"))];
        }
        let mut preread = vec![0; 65536];
        let n = ffi::pgx_preread(r, preread.as_mut_ptr(), preread.len());
        if n < 0 || ffi::pgx_upgrade(r) != 0 {
            return vec![Action::Close(r)];
        }
        preread.truncate(n as usize);
        // Do not retain the 64 KiB scratch allocation for every idle socket.
        preread.shrink_to_fit();
        self.ws.insert(id, ws::Client::new(r, preread));
        vec![Action::Arm(r, true, n > 0)]
    }
}

#[no_mangle]
pub extern "C" fn pgx_socket(id: u64) {
    run(|w| {
        let Some(mut c) = w.ws.remove(&id) else {
            return vec![];
        };
        let mut again = false;
        if !c.flush() {
            c.closing = true;
        }
        for count in 0..32 {
            if c.closing {
                break;
            }
            match c.socket.read() {
                Ok(Message::Text(text)) => {
                    let Ok(v) = serde_json::from_str::<Value>(&text) else {
                        c.closing = true;
                        break;
                    };
                    match v["type"].as_str() {
                        Some("auth") if c.claims.is_none() => {
                            match v["token"].as_str().ok_or(()).and_then(|t| w.auth.verify(t)) {
                                Ok(claims) => {
                                    c.claims = Some(claims);
                                    if !c.send(json!({"type":"ready"})) {
                                        c.closing = true;
                                    }
                                }
                                Err(()) => c.closing = true,
                            }
                        }
                        Some("subscribe") if c.claims.as_ref().is_some_and(|c| c.exp > now()) => {
                            let sid = v["id"].as_str().unwrap_or("");
                            let stream = w
                                .contract
                                .streams
                                .iter()
                                .position(|s| Some(s.name.as_str()) == v["stream"].as_str());
                            if sid.is_empty()
                                || sid.len() > 64
                                || c.subscriptions.contains_key(sid)
                                || c.subscriptions.len() >= 16
                                || stream.is_none()
                                || !w.listener_ready()
                            {
                                c.closing = true;
                                break;
                            }
                            let stream = stream.unwrap();
                            let params = v.get("params").cloned().unwrap_or(json!({}));
                            if w.stream_input[stream]
                                .as_ref()
                                .is_some_and(|schema| !schema.is_valid(&params))
                            {
                                c.closing = true;
                                break;
                            }
                            c.subscriptions.insert(
                                sid.to_owned(),
                                ws::Subscription {
                                    stream,
                                    params,
                                    key: None,
                                    pending: false,
                                    dirty: true,
                                    ready: true,
                                },
                            );
                            w.ready_ws.push_back((id, sid.to_owned()));
                        }
                        Some("unsubscribe") if c.claims.is_some() => {
                            if let Some(sid) = v["id"].as_str() {
                                // IDs cannot be recycled while an authorization job is running.
                                if c.subscriptions.get(sid).is_some_and(|s| s.pending) {
                                    c.closing = true;
                                    break;
                                }
                                c.subscriptions.remove(sid);
                                w.ready_ws.retain(|(client, subscription)| {
                                    *client != id || subscription != sid
                                });
                            }
                        }
                        _ => c.closing = true,
                    }
                }
                Ok(Message::Ping(_)) => {
                    if !c.flush() {
                        c.closing = true;
                    }
                }
                Ok(Message::Pong(_)) => c.pong_due = None,
                Ok(Message::Close(_)) => {
                    c.flush();
                    c.closing = true;
                }
                Ok(_) => c.closing = true,
                Err(tungstenite::Error::Io(e)) if e.kind() == ErrorKind::WouldBlock => break,
                Err(_) => {
                    c.closing = true;
                    break;
                }
            }
            if count == 31 {
                again = true;
            }
        }
        let action = if c.closing {
            Action::Close(c.request)
        } else {
            Action::Arm(c.request, c.needs_write, again)
        };
        w.ws.insert(id, c);
        let mut actions = vec![action];
        actions.extend(w.schedule());
        actions
    });
}
