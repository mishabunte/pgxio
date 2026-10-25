//! Private bindings to the opt-in patched libpq. Existing pq-sys stays unchanged.
use crate::input::{Input, Segment};
use pq_sys::{Oid, PGconn};
use std::ffi::{c_char, c_int, CStr};

#[repr(C)]
struct Param {
    segments: *const Segment,
    nsegments: usize,
    format: c_int,
    isnull: c_int,
}
extern "C" {
    fn PQsendQueryParamsBorrowed(
        conn: *mut PGconn,
        sql: *const c_char,
        n: c_int,
        types: *const Oid,
        params: *const Param,
        result_format: c_int,
    ) -> c_int;
    fn PQsendQueryPreparedBorrowed(
        conn: *mut PGconn,
        name: *const c_char,
        n: c_int,
        params: *const Param,
        result_format: c_int,
    ) -> c_int;
}

/// The connection retains submitted payloads independently of command results
/// until a complete flush or PQfinish.
pub unsafe fn send(conn: *mut PGconn, sql: &CStr, name: Option<&CStr>, values: &[Input]) -> bool {
    let segments: Vec<Vec<Segment>> = values
        .iter()
        .map(|v| match v {
            Input::Owned(v) => vec![Segment {
                data: v.as_ptr().cast(),
                len: v.as_bytes().len(),
            }],
            Input::Borrowed(_) => v.segments().to_vec(),
            Input::Submitted => panic!("cannot submit a parameter twice"),
        })
        .collect();
    let params: Vec<_> = segments
        .iter()
        .map(|s| Param {
            segments: s.as_ptr(),
            nsegments: s.len(),
            format: 0,
            isnull: 0,
        })
        .collect();
    match name {
        Some(name) => {
            PQsendQueryPreparedBorrowed(
                conn,
                name.as_ptr(),
                params.len() as c_int,
                params.as_ptr(),
                0,
            ) == 1
        }
        None => {
            PQsendQueryParamsBorrowed(
                conn,
                sql.as_ptr(),
                params.len() as c_int,
                std::ptr::null(),
                params.as_ptr(),
                0,
            ) == 1
        }
    }
}
