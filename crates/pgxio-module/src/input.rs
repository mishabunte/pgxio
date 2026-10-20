//! Owned REST input or an explicit worker-local lease on Nginx body segments.
use std::ffi::{CStr, CString};

pub const MAX_BODY: usize = 65_536;
pub const MAX_SEGMENTS: usize = 1024;

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct Segment {
    pub data: *const u8,
    pub len: usize,
}

/// Worker-local lease: the held HTTP request owns these immutable bytes.
/// Its cleanup hook must dispose of any libpq references before pool teardown.
pub struct BorrowedBody {
    pub client: u64,
    segments: Vec<Segment>,
}
pub enum Input {
    Owned(CString),
    Borrowed(BorrowedBody),
    /// Submitted bytes are held by the connection until its output is flushed.
    Submitted,
}
impl From<CString> for Input {
    fn from(value: CString) -> Self {
        Self::Owned(value)
    }
}
impl Input {
    pub fn borrowed(&self) -> bool {
        matches!(self, Self::Borrowed(_))
    }
    pub fn segments(&self) -> &[Segment] {
        match self {
            Self::Borrowed(body) => &body.segments,
            _ => &[],
        }
    }
    pub fn owned(&self) -> &CString {
        match self {
            Self::Owned(value) => value,
            _ => panic!("parameter is not owned"),
        }
    }
    /// Copy while the original request lease is still alive. Used when an auto
    /// candidate acquires a connection whose transport changed while queued.
    pub fn into_owned_copy(self) -> Result<Self, Error> {
        if !self.borrowed() { return Ok(self); }
        let length: usize = self.segments().iter().map(|p| p.len).sum();
        unsafe {
            copy_body(length as isize, |dst, _| {
                let mut offset = 0;
                for part in self.segments() {
                    if part.len > 0 { std::ptr::copy_nonoverlapping(part.data, dst.add(offset), part.len); }
                    offset += part.len;
                }
                offset as isize
            }).map(Into::into)
        }
    }
    /// # Safety
    /// All nonempty segments must remain immutable and readable until this
    /// lease is dropped, including while libpq retains their pointers.
    pub unsafe fn borrow(client: u64, segments: Vec<Segment>) -> Result<Self, Error> {
        if segments.len() > MAX_SEGMENTS {
            return Err(Error::TooLarge);
        }
        let mut total = 0usize;
        for part in &segments {
            if part.len > MAX_BODY - total {
                return Err(Error::TooLarge);
            }
            total += part.len;
            if part.len != 0 {
                if part.data.is_null()
                    || std::slice::from_raw_parts(part.data, part.len).contains(&0)
                {
                    return Err(Error::InvalidJson);
                }
            }
        }
        Ok(Self::Borrowed(BorrowedBody { client, segments }))
    }
}

#[derive(Debug, PartialEq)]
pub enum Error {
    TooLarge,
    InvalidJson,
    InvalidInput,
}

/// Copy into one body-sized allocation with space for libpq's text terminator.
///
/// # Safety
/// `fill` must initialize exactly the number of bytes it returns, without writing
/// beyond `capacity`. It must not retain the destination pointer.
pub unsafe fn copy_body(
    length: isize,
    fill: impl FnOnce(*mut u8, usize) -> isize,
) -> Result<CString, Error> {
    if length == -2 || length > MAX_BODY as isize {
        return Err(Error::TooLarge);
    }
    if length < 0 {
        return Err(Error::InvalidJson);
    }
    let length = length as usize;
    let mut bytes = Vec::<u8>::with_capacity(length + 1);
    if fill(bytes.as_mut_ptr(), length) != length as isize {
        return Err(Error::InvalidJson);
    }
    // The FFI writer has initialized the complete body. No spare byte is read.
    bytes.set_len(length);
    bytes.push(0);
    CString::from_vec_with_nul(bytes).map_err(|_| Error::InvalidJson)
}

pub fn check_schema(bytes: &CStr, schema: &jsonschema::Validator) -> Result<(), Error> {
    let value: serde_json::Value =
        serde_json::from_slice(bytes.to_bytes()).map_err(|_| Error::InvalidJson)?;
    if schema.is_valid(&value) {
        Ok(())
    } else {
        Err(Error::InvalidInput)
    }
}
