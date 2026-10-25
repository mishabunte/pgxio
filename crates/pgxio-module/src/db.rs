//! Nonblocking libpq sessions. One transaction per connection; results must be
//! drained and the session idle before another caller can acquire it.
use crate::ffi;
use crate::buffer::BufferMode;
use crate::input::Input;
use pq_sys::*;
use serde_json::{json, Value};
use std::{
    collections::{HashMap, VecDeque},
    ffi::{c_void, CStr, CString},
    ptr,
    sync::Arc,
    time::{Duration, Instant},
};

pub struct Job {
    pub client: u64,
    pub subscription: Option<String>,
    pub function: String,
    pub input: Option<Input>,
    pub subject: String,
    pub expires: u64,
    pub read_only: bool,
    pub output: Option<Arc<jsonschema::Validator>>,
}
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Error {
    Database,
    InvalidInput,
}
impl Error {
    pub fn from_sqlstate(code: &[u8]) -> Self {
        match code {
            b"22P02" | b"22021" | b"22P05" | b"22003" | b"PT400" => Self::InvalidInput,
            _ => Self::Database,
        }
    }
}
pub enum Output {
    Http(Vec<u8>),
    Internal(Value),
}
pub enum Event {
    Transport(usize, bool),
    AutoCopied,
    Done(Job, Result<Output, Error>),
    Notify(String),
    ListenerLost,
}
#[derive(Clone, Copy, PartialEq)]
enum State {
    Offline,
    Connecting,
    Audit,
    Listen,
    Idle,
    Executing,
    Commit,
    Rollback,
    Reset,
    Cancelled,
    Listening,
}
#[derive(Clone)]
enum Kind {
    Control,
    Audit,
    Settings,
    Output,
    Prepare(String, String),
}
struct Command {
    kind: Kind,
    sql: CString,
    name: Option<CString>,
    params: Vec<Input>,
}
impl Command {
    fn query(kind: Kind, sql: &str, params: Vec<Input>) -> Self {
        Self {
            kind,
            sql: CString::new(sql).unwrap(),
            name: None,
            params,
        }
    }
}
const SETTINGS: &str = "SELECT jsonb_build_object('claims',set_config('request.jwt.claims',$1,true),'timeout',set_config('statement_timeout','4000',true),'lock',set_config('lock_timeout','1000',true),'path',set_config('search_path','pg_catalog,web',true))";
const AUDIT: &str = "SELECT (current_user='web' AND session_user='web' AND NOT r.rolsuper AND NOT r.rolcreatedb AND NOT r.rolcreaterole AND NOT r.rolreplication AND NOT r.rolbypassrls AND NOT has_schema_privilege('web','CREATE') AND NOT EXISTS (SELECT 1 FROM pg_auth_members WHERE member=r.oid))::text FROM pg_roles r WHERE rolname=current_user";
pub struct Connection {
    buffer_mode: BufferMode,
    raw: *mut PGconn,
    watcher: *mut c_void,
    fd: i32,
    id: usize,
    listener: bool,
    state: State,
    job: Option<Job>,
    value: Option<Value>,
    http_output: Option<Vec<u8>>,
    failed: bool,
    job_error: Error,
    deadline: Instant,
    retry: Duration,
    commands: VecDeque<Command>,
    // A command can finish with an early parse/pipeline error before PQflush
    // consumes its bytes. These leases must outlive command/result bookkeeping.
    borrowed_inputs: Vec<Input>,
    response_seen: bool,
    pipeline: bool,
    sync_seen: bool,
    prepare_enabled: bool,
    pipeline_enabled: bool,
    prepared: HashMap<String, String>,
    serial: u64,
    reset_prepared: bool,
    cancel: *mut PGcancelConn,
    cancel_watcher: *mut c_void,
    cancel_fd: i32,
    cancelled: bool,
}
/// Avoid synchronous DNS inside PQconnectPoll. The original host remains in
/// conninfo for certificate verification; the CLI supplies a numeric hostaddr.
pub fn validate_url(url: &str, development: bool) -> Result<CString, String> {
    let url = CString::new(url).map_err(|_| "invalid database URL")?;
    unsafe {
        let mut err = ptr::null_mut();
        let options = PQconninfoParse(url.as_ptr(), &mut err);
        if options.is_null() {
            if !err.is_null() {
                PQfreemem(err.cast());
            }
            return Err("invalid database URL".into());
        }
        let mut hostaddr = String::new();
        let mut sslmode = String::new();
        let mut user = String::new();
        let mut p = options;
        while !(*p).keyword.is_null() {
            if !(*p).val.is_null() {
                let v = CStr::from_ptr((*p).val).to_string_lossy().into_owned();
                match CStr::from_ptr((*p).keyword).to_bytes() {
                    b"hostaddr" => hostaddr = v,
                    b"sslmode" => sslmode = v,
                    b"user" => user = v,
                    _ => {}
                }
            }
            p = p.add(1);
        }
        PQconninfoFree(options);
        if user != "web" && !user.starts_with("web.") {
            return Err("database URL must use the web role".into());
        }
        if hostaddr.parse::<std::net::IpAddr>().is_err() {
            return Err("database URL requires numeric hostaddr (run pgxio resolve-url)".into());
        }
        if !development && sslmode != "verify-full" {
            return Err("production requires sslmode=verify-full".into());
        }
        Ok(url)
    }
}

impl Connection {
    pub fn new(id: usize, listener: bool, prepared: bool, pipeline: bool, buffer_mode: BufferMode) -> Self {
        Self {
            buffer_mode,
            raw: ptr::null_mut(),
            watcher: ptr::null_mut(),
            fd: -1,
            id,
            listener,
            state: State::Offline,
            job: None,
            value: None,
            http_output: None,
            failed: false,
            job_error: Error::Database,
            deadline: Instant::now(),
            retry: Duration::from_secs(1),
            commands: VecDeque::new(),
            borrowed_inputs: Vec::new(),
            response_seen: false,
            pipeline: false,
            sync_seen: false,
            prepare_enabled: prepared,
            pipeline_enabled: pipeline,
            prepared: HashMap::new(),
            serial: 0,
            reset_prepared: false,
            cancel: ptr::null_mut(),
            cancel_watcher: ptr::null_mut(),
            cancel_fd: -1,
            cancelled: false,
        }
    }
    pub fn idle(&self) -> bool {
        self.state == State::Idle && self.borrowed_inputs.is_empty()
    }
    fn plaintext(&self) -> bool {
        unsafe {
            !self.raw.is_null() && PQstatus(self.raw) == ConnStatusType::CONNECTION_OK
                && PQsslInUse(self.raw) == 0 && PQgssEncInUse(self.raw) == 0
        }
    }
    pub fn usable(&self) -> bool {
        !self.listener && matches!(self.state, State::Idle | State::Executing | State::Commit)
    }
    pub fn listening(&self) -> bool {
        self.state == State::Listening
    }
    pub fn references(&self, client: u64) -> bool {
        self.job.as_ref().is_some_and(|j| j.client == client)
            || self
                .borrowed_inputs
                .iter()
                .any(|input| matches!(input, Input::Borrowed(body) if body.client == client))
    }
    pub fn next_deadline(&self) -> Option<Instant> {
        (!matches!(self.state, State::Idle | State::Listening)).then_some(self.deadline)
    }
    pub fn tick(&mut self, url: &CStr) -> Vec<Event> {
        if self.next_deadline().is_none_or(|d| Instant::now() < d) {
            return vec![];
        }
        if self.state == State::Offline {
            self.raw = unsafe {
                let keywords = [c"dbname".as_ptr(), c"client_encoding".as_ptr(), ptr::null()];
                let values = [url.as_ptr(), c"UTF8".as_ptr(), ptr::null()];
                PQconnectStartParams(keywords.as_ptr(), values.as_ptr(), 1)
            };
            if self.raw.is_null() {
                self.deadline = Instant::now() + self.retry;
                return vec![];
            }
            self.state = State::Connecting;
            self.deadline = Instant::now() + Duration::from_secs(10);
            self.advance()
        } else if matches!(self.state, State::Executing | State::Commit) && !self.cancelled {
            self.cancel()
        } else {
            self.disconnect()
        }
    }
    fn clear_cancel(&mut self) {
        unsafe {
            ffi::pgx_watch_free(self.cancel_watcher);
            self.cancel_watcher = ptr::null_mut();
            self.cancel_fd = -1;
            if !self.cancel.is_null() {
                PQcancelFinish(self.cancel);
                self.cancel = ptr::null_mut();
            }
        }
    }
    pub fn disconnect(&mut self) -> Vec<Event> {
        let mut events = vec![];
        if !self.listener { events.push(Event::Transport(self.id, false)); }
        if self.listener && self.state != State::Offline {
            events.push(Event::ListenerLost);
        }
        if let Some(job) = self.job.take() {
            events.push(Event::Done(job, Err(Error::Database)));
        }
        self.clear_cancel();
        unsafe {
            ffi::pgx_watch_free(self.watcher);
            self.watcher = ptr::null_mut();
            self.fd = -1;
            if !self.raw.is_null() {
                PQfinish(self.raw);
                self.raw = ptr::null_mut();
            }
        }
        self.state = State::Offline;
        self.value = None;
        self.http_output = None;
        self.commands.clear();
        self.borrowed_inputs.clear();
        self.prepared.clear();
        self.pipeline = false;
        self.cancelled = false;
        self.reset_prepared = false;
        self.deadline = Instant::now() + self.retry;
        self.retry = (self.retry * 2).min(Duration::from_secs(30));
        events
    }
    unsafe fn arm(&mut self, read: bool, write: bool) -> bool {
        let fd = PQsocket(self.raw);
        if fd < 0 {
            return false;
        }
        if fd != self.fd {
            ffi::pgx_watch_free(self.watcher);
            self.watcher = ffi::pgx_watch_new(fd, self.id);
            self.fd = fd;
        }
        !self.watcher.is_null() && ffi::pgx_watch_arm(self.watcher, read.into(), write.into()) == 0
    }
    unsafe fn dispatch(raw: *mut PGconn, command: &mut Command, held: &mut Vec<Input>) -> bool {
        #[cfg(not(feature = "borrowed-libpq"))]
        let _ = held;
        #[cfg(feature = "borrowed-libpq")]
        if command.params.iter().any(Input::borrowed) {
            let submitted =
                crate::borrowed::send(raw, &command.sql, command.name.as_deref(), &command.params);
            if submitted {
                for param in &mut command.params {
                    // The borrowed API retains every payload, including owned
                    // parameters mixed with Nginx segments in the same call.
                    held.push(std::mem::replace(param, Input::Submitted));
                }
            }
            return submitted;
        }
        let params: Vec<_> = command.params.iter().map(|p| p.owned().as_ptr()).collect();
        match &command.kind {
            Kind::Prepare(_, name) => {
                let name = CString::new(name.as_str()).unwrap();
                PQsendPrepare(raw, name.as_ptr(), command.sql.as_ptr(), 1, ptr::null()) == 1
            }
            _ => {
                if let Some(name) = &command.name {
                    PQsendQueryPrepared(
                        raw,
                        name.as_ptr(),
                        params.len() as i32,
                        params.as_ptr(),
                        ptr::null(),
                        ptr::null(),
                        0,
                    ) == 1
                } else {
                    PQsendQueryParams(
                        raw,
                        command.sql.as_ptr(),
                        params.len() as i32,
                        ptr::null(),
                        params.as_ptr(),
                        ptr::null(),
                        ptr::null(),
                        0,
                    ) == 1
                }
            }
        }
    }
    unsafe fn flush(&mut self) -> i32 {
        let result = PQflush(self.raw);
        if result == 0 {
            self.borrowed_inputs.clear();
        }
        result
    }
    fn batch(&mut self, commands: VecDeque<Command>, pipeline: bool) -> bool {
        self.commands = commands;
        self.response_seen = false;
        self.sync_seen = false;
        self.failed = false;
        unsafe {
            self.pipeline = pipeline;
            if pipeline {
                if PQenterPipelineMode(self.raw) != 1 {
                    return false;
                }
                for command in &mut self.commands {
                    if !Self::dispatch(self.raw, command, &mut self.borrowed_inputs) {
                        return false;
                    }
                }
                if PQpipelineSync(self.raw) != 1 {
                    return false;
                }
            } else if !Self::dispatch(
                self.raw,
                self.commands.front_mut().unwrap(),
                &mut self.borrowed_inputs,
            ) {
                return false;
            }
            let flush = self.flush();
            flush >= 0 && self.arm(true, flush == 1)
        }
    }
    fn control(&mut self, sql: &str) -> bool {
        self.batch(
            VecDeque::from([Command::query(Kind::Control, sql, vec![])]),
            false,
        )
    }
    fn append_query(
        &mut self,
        commands: &mut VecDeque<Command>,
        key: &str,
        sql: &str,
        input: Input,
        kind: Kind,
    ) {
        let name = if self.prepare_enabled {
            Some(if let Some(name) = self.prepared.get(key) {
                name.clone()
            } else {
                self.serial += 1;
                let name = format!("pgxio_{}", self.serial);
                commands.push_back(Command::query(
                    Kind::Prepare(key.to_owned(), name.clone()),
                    sql,
                    vec![],
                ));
                name
            })
        } else {
            None
        };
        let mut command = Command::query(kind, sql, vec![input]);
        command.name = name.map(|n| CString::new(n).unwrap());
        commands.push_back(command);
    }
    pub fn start(&mut self, mut job: Job) -> Vec<Event> {
        assert!(self.idle());
        let mut events = vec![];
        if self.buffer_mode == BufferMode::Auto && job.input.as_ref().is_some_and(Input::borrowed) && !self.plaintext() {
            let input = job.input.take().unwrap();
            match input.into_owned_copy() {
                Ok(input) => job.input = Some(input),
                Err(_) => return vec![Event::Done(job, Err(Error::InvalidInput))],
            }
            events.push(Event::AutoCopied);
        }
        if let Some(Input::Borrowed(body)) = &job.input {
            assert_eq!(body.client, job.client, "body lease belongs to its request");
        }
        let mut commands = VecDeque::from([Command::query(
            Kind::Control,
            if job.read_only {
                "BEGIN READ ONLY"
            } else {
                "BEGIN"
            },
            vec![],
        )]);
        self.append_query(
            &mut commands,
            "settings",
            SETTINGS,
            CString::new(json!({"sub":job.subject,"role":"web"}).to_string())
                .unwrap()
                .into(),
            Kind::Settings,
        );
        self.append_query(
            &mut commands,
            &job.function,
            &format!("SELECT {}($1::jsonb)", job.function),
            job.input.take().expect("admitted job has input"),
            Kind::Output,
        );
        self.job = Some(job);
        self.job_error = Error::Database;
        self.value = None;
        self.http_output = None;
        self.cancelled = false;
        self.reset_prepared = false;
        self.state = State::Executing;
        self.deadline = Instant::now() + Duration::from_secs(6);
        if self.batch(commands, self.pipeline_enabled) {
            events
        } else {
            self.disconnect()
        }
    }
    pub fn cancel(&mut self) -> Vec<Event> {
        if !self.borrowed_inputs.is_empty()
            || self
                .commands
                .iter()
                .any(|c| c.params.iter().any(Input::borrowed))
        {
            return self.disconnect();
        }
        if !matches!(self.state, State::Executing | State::Commit) || self.cancelled {
            return vec![];
        }
        self.cancelled = true;
        self.deadline = Instant::now() + Duration::from_secs(1);
        unsafe {
            self.cancel = PQcancelCreate(self.raw);
            if self.cancel.is_null() || PQcancelStart(self.cancel) != 1 {
                return self.disconnect();
            }
        }
        self.advance()
    }
    fn rollback(&mut self) -> Vec<Event> {
        self.value = None;
        self.http_output = None;
        if !self.cancel.is_null() {
            self.state = State::Cancelled;
            return vec![];
        }
        self.state = State::Rollback;
        self.deadline = Instant::now() + Duration::from_secs(1);
        if self.control("ROLLBACK") {
            vec![]
        } else {
            self.disconnect()
        }
    }
    fn finish_job(&mut self, success: bool) -> Vec<Event> {
        self.state = State::Idle;
        self.cancelled = false;
        self.job
            .take()
            .map(|job| {
                Event::Done(
                    job,
                    if success {
                        Ok(if let Some(bytes) = self.http_output.take() {
                            Output::Http(bytes)
                        } else {
                            Output::Internal(self.value.take().unwrap_or(Value::Null))
                        })
                    } else {
                        self.http_output = None;
                        self.value = None;
                        Err(self.job_error)
                    },
                )
            })
            .into_iter()
            .collect()
    }
    fn finish_batch(&mut self) -> Vec<Event> {
        match self.state {
            State::Audit => {
                if self.failed || self.value != Some(Value::Bool(true)) {
                    return self.disconnect();
                }
                self.retry = Duration::from_secs(1);
                if self.listener {
                    self.state = State::Listen;
                    if !self.control("LISTEN pgxio_events") {
                        return self.disconnect();
                    }
                } else {
                    self.state = State::Idle;
                    return vec![Event::Transport(self.id, self.plaintext())];
                }
                vec![]
            }
            State::Listen => {
                if self.failed {
                    return self.disconnect();
                }
                self.retry = Duration::from_secs(1);
                self.state = State::Listening;
                vec![]
            }
            State::Executing => {
                let job = self.job.as_ref().unwrap();
                let valid = !self.failed
                    && !self.cancelled
                    && job.expires > crate::now()
                    && (if job.subscription.is_none() {
                        self.http_output.is_some()
                    } else {
                        self.value.is_some()
                    });
                if !valid {
                    return self.rollback();
                }
                self.state = State::Commit;
                if self.control("COMMIT") {
                    vec![]
                } else {
                    self.disconnect()
                }
            }
            State::Commit => {
                if self.failed || self.cancelled {
                    return self.rollback();
                }
                if unsafe { PQtransactionStatus(self.raw) } != PGTransactionStatusType::PQTRANS_IDLE
                {
                    return self.disconnect();
                }
                self.finish_job(true)
            }
            State::Rollback => {
                if self.failed
                    || unsafe { PQtransactionStatus(self.raw) }
                        != PGTransactionStatusType::PQTRANS_IDLE
                {
                    return self.disconnect();
                }
                if self.reset_prepared {
                    self.state = State::Reset;
                    self.deadline = Instant::now() + Duration::from_secs(1);
                    if !self.control("DEALLOCATE ALL") {
                        return self.disconnect();
                    }
                    vec![]
                } else {
                    self.finish_job(false)
                }
            }
            State::Reset => {
                if self.failed {
                    return self.disconnect();
                }
                self.prepared.clear();
                self.reset_prepared = false;
                self.finish_job(false)
            }
            _ => vec![],
        }
    }
    unsafe fn result(&mut self, result: *mut PGresult) {
        let status = PQresultStatus(result);
        if status == ExecStatusType::PGRES_PIPELINE_SYNC {
            self.sync_seen = true;
            return;
        }
        self.response_seen = true;
        if status != ExecStatusType::PGRES_COMMAND_OK && status != ExecStatusType::PGRES_TUPLES_OK {
            self.failed = true;
            let code = PQresultErrorField(result, PG_DIAG_SQLSTATE as i32);
            if !code.is_null()
                && self.state == State::Executing
                && status != ExecStatusType::PGRES_PIPELINE_ABORTED
                && matches!(self.commands.front().map(|c| &c.kind), Some(Kind::Output))
            {
                self.job_error = Error::from_sqlstate(CStr::from_ptr(code).to_bytes());
            }
            if !code.is_null()
                && matches!(
                    CStr::from_ptr(code).to_bytes(),
                    b"26000" | b"0A000" | b"42P05"
                )
            {
                self.reset_prepared = true;
            }
            return;
        }
        match self.commands.front().map(|c| &c.kind) {
            Some(Kind::Prepare(key, name)) if status == ExecStatusType::PGRES_COMMAND_OK => {
                self.prepared.insert(key.clone(), name.clone());
            }
            Some(Kind::Audit | Kind::Output) => {
                if status != ExecStatusType::PGRES_TUPLES_OK
                    || PQntuples(result) != 1
                    || PQnfields(result) != 1
                    || PQgetisnull(result, 0, 0) != 0
                {
                    self.failed = true;
                    return;
                }
                let length = PQgetlength(result, 0, 0) as usize;
                if length > 1_048_576 {
                    self.failed = true;
                    return;
                }
                let bytes =
                    std::slice::from_raw_parts(PQgetvalue(result, 0, 0).cast::<u8>(), length);
                let http = matches!(self.commands.front().map(|c| &c.kind), Some(Kind::Output))
                    && self.job.as_ref().is_some_and(|j| j.subscription.is_none());
                if http {
                    // PostgreSQL guarantees JSONB syntax; never treat arbitrary text as JSON.
                    if PQftype(result, 0) != 3802 || PQfformat(result, 0) != 0 {
                        self.failed = true;
                        return;
                    }
                    let schema = self.job.as_ref().and_then(|j| j.output.as_deref());
                    match crate::output::capture(bytes, schema) {
                        Ok(body) => self.http_output = Some(body),
                        Err(()) => self.failed = true,
                    }
                } else {
                    self.value = serde_json::from_slice(bytes).ok();
                    if self.value.is_none() {
                        self.failed = true;
                    }
                }
            }
            Some(Kind::Control) if status == ExecStatusType::PGRES_COMMAND_OK => {}
            Some(Kind::Settings) if status == ExecStatusType::PGRES_TUPLES_OK => {}
            _ => self.failed = true,
        }
    }
    pub fn advance(&mut self) -> Vec<Event> {
        if self.raw.is_null() {
            return vec![];
        }
        unsafe {
            if !self.cancel.is_null() {
                let polling = PQcancelPoll(self.cancel);
                match polling {
                    PostgresPollingStatusType::PGRES_POLLING_OK => {
                        self.clear_cancel();
                        if self.state == State::Cancelled {
                            return self.rollback();
                        }
                    }
                    PostgresPollingStatusType::PGRES_POLLING_READING
                    | PostgresPollingStatusType::PGRES_POLLING_WRITING => {
                        let fd = PQcancelSocket(self.cancel);
                        if fd < 0 {
                            return self.disconnect();
                        }
                        if fd != self.cancel_fd {
                            ffi::pgx_watch_free(self.cancel_watcher);
                            self.cancel_watcher = ffi::pgx_watch_new(fd, self.id);
                            self.cancel_fd = fd;
                        }
                        if self.cancel_watcher.is_null()
                            || ffi::pgx_watch_arm(
                                self.cancel_watcher,
                                (polling == PostgresPollingStatusType::PGRES_POLLING_READING)
                                    .into(),
                                (polling == PostgresPollingStatusType::PGRES_POLLING_WRITING)
                                    .into(),
                            ) != 0
                        {
                            return self.disconnect();
                        }
                    }
                    _ => return self.disconnect(),
                }
            }
            if self.state == State::Connecting {
                match PQconnectPoll(self.raw) {
                    PostgresPollingStatusType::PGRES_POLLING_READING => {
                        if self.arm(true, false) {
                            return vec![];
                        }
                    }
                    PostgresPollingStatusType::PGRES_POLLING_WRITING => {
                        if self.arm(false, true) {
                            return vec![];
                        }
                    }
                    PostgresPollingStatusType::PGRES_POLLING_OK => {
                        let encoding = PQparameterStatus(self.raw, c"client_encoding".as_ptr());
                        if encoding.is_null()
                            || CStr::from_ptr(encoding).to_bytes() != b"UTF8"
                            || PQsetnonblocking(self.raw, 1) != 0
                        {
                            return self.disconnect();
                        }
                        self.state = State::Audit;
                        if self.batch(
                            VecDeque::from([Command::query(Kind::Audit, AUDIT, vec![])]),
                            false,
                        ) {
                            return vec![];
                        }
                    }
                    _ => {}
                }
                return self.disconnect();
            }
            let flush = self.flush();
            if flush < 0 || PQconsumeInput(self.raw) != 1 || !self.arm(true, flush == 1) {
                return self.disconnect();
            }
            let mut events = vec![];
            if self.listener {
                loop {
                    let notify = PQnotifies(self.raw);
                    if notify.is_null() {
                        break;
                    }
                    let key = CStr::from_ptr((*notify).extra).to_string_lossy();
                    if key.len() <= 128 {
                        events.push(Event::Notify(key.into_owned()));
                    }
                    PQfreemem(notify.cast());
                }
            }
            if matches!(
                self.state,
                State::Idle | State::Listening | State::Cancelled
            ) {
                return events;
            }
            loop {
                if PQisBusy(self.raw) != 0 {
                    return events;
                }
                let result = PQgetResult(self.raw);
                if !result.is_null() {
                    self.result(result);
                    PQclear(result);
                    continue;
                }
                if self.response_seen {
                    self.commands.pop_front();
                    self.response_seen = false;
                    if !self.pipeline {
                        if self.failed {
                            self.commands.clear();
                        }
                        if let Some(command) = self.commands.front_mut() {
                            if !Self::dispatch(self.raw, command, &mut self.borrowed_inputs) {
                                return self.disconnect();
                            }
                            let flush = self.flush();
                            if flush < 0 || !self.arm(true, flush == 1) {
                                return self.disconnect();
                            }
                            return events;
                        }
                        events.extend(self.finish_batch());
                        return events;
                    }
                    continue;
                }
                if self.pipeline && self.sync_seen {
                    if !self.commands.is_empty() || PQexitPipelineMode(self.raw) != 1 {
                        return self.disconnect();
                    }
                    self.pipeline = false;
                    events.extend(self.finish_batch());
                }
                return events;
            }
        }
    }
}
impl Drop for Connection {
    fn drop(&mut self) {
        self.disconnect();
    }
}
