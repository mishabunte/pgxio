use crate::{
    auth::Claims,
    ffi::{self, Request},
};
use serde_json::Value;
use std::{
    collections::HashMap,
    io::{self, Read, Write},
    time::Instant,
};
use tungstenite::{
    protocol::{Role, WebSocketConfig},
    Message, WebSocket,
};

pub struct Transport(pub Request);
fn result(n: isize) -> io::Result<usize> {
    match n {
        -2 => Err(io::ErrorKind::WouldBlock.into()),
        n if n < 0 => Err(io::ErrorKind::BrokenPipe.into()),
        n => Ok(n as usize),
    }
}
impl Read for Transport {
    fn read(&mut self, b: &mut [u8]) -> io::Result<usize> {
        result(unsafe { ffi::pgx_recv(self.0, b.as_mut_ptr(), b.len()) })
    }
}
impl Write for Transport {
    fn write(&mut self, b: &[u8]) -> io::Result<usize> {
        result(unsafe { ffi::pgx_send(self.0, b.as_ptr(), b.len()) })
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
pub struct Subscription {
    pub stream: usize,
    pub params: Value,
    pub key: Option<String>,
    pub pending: bool,
    pub dirty: bool,
    pub ready: bool,
}
pub struct Client {
    pub request: Request,
    pub socket: WebSocket<Transport>,
    pub claims: Option<Claims>,
    pub subscriptions: HashMap<String, Subscription>,
    pub created: Instant,
    pub last_ping: Instant,
    pub pong_due: Option<Instant>,
    pub needs_write: bool,
    pub closing: bool,
}
impl Client {
    pub fn new(request: Request, preread: Vec<u8>) -> Self {
        let config = WebSocketConfig::default()
            .read_buffer_size(4096)
            .write_buffer_size(0)
            .max_write_buffer_size(65536)
            .max_message_size(Some(16384))
            .max_frame_size(Some(16384));
        Self {
            request,
            socket: WebSocket::from_partially_read(
                Transport(request),
                preread,
                Role::Server,
                Some(config),
            ),
            claims: None,
            subscriptions: HashMap::new(),
            created: Instant::now(),
            last_ping: Instant::now(),
            pong_due: None,
            needs_write: false,
            closing: false,
        }
    }
    pub fn send(&mut self, v: Value) -> bool {
        self.write(Message::Text(v.to_string().into()))
    }
    pub fn write(&mut self, m: Message) -> bool {
        match self.socket.send(m) {
            Ok(()) => {
                self.needs_write = false;
                true
            }
            Err(tungstenite::Error::Io(e)) if e.kind() == io::ErrorKind::WouldBlock => {
                self.needs_write = true;
                true
            }
            _ => false,
        }
    }
    pub fn flush(&mut self) -> bool {
        match self.socket.flush() {
            Ok(()) => {
                self.needs_write = false;
                true
            }
            Err(tungstenite::Error::Io(e)) if e.kind() == io::ErrorKind::WouldBlock => {
                self.needs_write = true;
                true
            }
            _ => false,
        }
    }
}
