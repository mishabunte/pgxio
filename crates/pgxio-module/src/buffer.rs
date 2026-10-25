//! Request input policy. Transport state is updated only on connection events.
use serde::Deserialize;

pub const AUTO_MIN_BYTES: usize = 16 * 1024;
pub const AUTO_BYTES_PER_SEGMENT: usize = 4 * 1024;

#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum BufferMode {
    Copy,
    Borrowed,
    #[default]
    Auto,
}
impl BufferMode {
    pub fn may_borrow(self, patched: bool, schema: bool, all_plaintext: bool) -> bool {
        patched && !schema && match self {
            Self::Copy => false,
            Self::Borrowed => true,
            Self::Auto => all_plaintext,
        }
    }
    pub fn select(self, bytes: usize, segments: usize, patched: bool, schema: bool, plaintext: bool) -> bool {
        self.may_borrow(patched, schema, plaintext)
            && segments > 0 && segments <= crate::input::MAX_SEGMENTS
            && (self != Self::Auto || (bytes >= AUTO_MIN_BYTES && segments <= bytes / AUTO_BYTES_PER_SEGMENT))
    }
}

#[derive(Default)]
pub struct PlaintextPool(u64);
impl PlaintextPool {
    pub fn update(&mut self, index: usize, plaintext: bool) {
        if index < 64 {
            let bit = 1u64 << index;
            if plaintext { self.0 |= bit; } else { self.0 &= !bit; }
        }
    }
    pub fn all_plaintext(&self, count: usize) -> bool {
        count > 0 && count <= 64 && self.0.count_ones() as usize == count
    }
}
