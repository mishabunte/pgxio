use std::ffi::{c_char, c_void};
pub type Request = *mut c_void;
extern "C" {
    pub fn pgx_create_conf(cf: *mut ngx::ffi::ngx_conf_t) -> *mut c_void;
    pub fn pgx_init_conf(cf: *mut ngx::ffi::ngx_conf_t, conf: *mut c_void) -> *mut c_char;
    pub static mut pgx_commands: ngx::ffi::ngx_command_t;
    pub fn pgx_init_process(cycle: *mut ngx::ffi::ngx_cycle_t) -> ngx::ffi::ngx_int_t;
    pub fn pgx_exit_process(cycle: *mut ngx::ffi::ngx_cycle_t);
    pub fn pgx_stopping() -> i32;
    pub fn pgx_schedule_tick(delay_ms: u64);
    pub fn pgx_attach(r: Request, id: u64) -> i32;
    pub fn pgx_id(r: Request) -> u64;
    pub fn pgx_uri(r: Request, len: *mut usize) -> *const u8;
    pub fn pgx_method(r: Request, len: *mut usize) -> *const u8;
    pub fn pgx_has_args(r: Request) -> i32;
    pub fn pgx_can_upgrade(r: Request) -> i32;
    pub fn pgx_header(r: Request, name: *const c_char, n: usize, len: *mut usize) -> *const u8;
    pub fn pgx_read_body(r: Request, single: i32) -> isize;
    pub fn pgx_wait_client(r: Request);
    pub fn pgx_body_length(r: Request) -> isize;
    pub fn pgx_body_segments(r: Request, dst: *mut crate::input::Segment, cap: usize) -> isize;
    pub fn pgx_copy_body(r: Request, dst: *mut u8, cap: usize) -> isize;
    pub fn pgx_add_header(r: Request, key: *const c_char, value: *const u8, len: usize) -> i32;
    pub fn pgx_reply(r: Request, status: i32, data: *const u8, len: usize);
    pub fn pgx_hold(r: Request);
    pub fn pgx_upgrade(r: Request) -> i32;
    pub fn pgx_preread(r: Request, buf: *mut u8, cap: usize) -> isize;
    pub fn pgx_recv(r: Request, buf: *mut u8, len: usize) -> isize;
    pub fn pgx_send(r: Request, buf: *const u8, len: usize) -> isize;
    pub fn pgx_ws_arm(r: Request, write: i32, again: i32);
    pub fn pgx_close(r: Request);
    pub fn pgx_watch_new(fd: i32, id: usize) -> *mut c_void;
    pub fn pgx_watch_arm(w: *mut c_void, read: i32, write: i32) -> i32;
    pub fn pgx_watch_free(w: *mut c_void);
}

pub unsafe fn header(r: Request, name: &str) -> String {
    let mut n = 0;
    let p = pgx_header(r, name.as_ptr().cast(), name.len(), &mut n);
    if p.is_null() {
        String::new()
    } else {
        String::from_utf8_lossy(std::slice::from_raw_parts(p, n)).into_owned()
    }
}
pub unsafe fn add_header(r: Request, key: &std::ffi::CStr, value: &str) {
    pgx_add_header(r, key.as_ptr(), value.as_ptr(), value.len());
}
