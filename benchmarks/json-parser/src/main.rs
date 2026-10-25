use serde::Deserialize;
use serde_json::{json, value::RawValue, Value};
use std::{
    alloc::{GlobalAlloc, Layout, System},
    borrow::Cow,
    cell::Cell,
    hint::black_box,
    time::Instant,
};

struct Counting;
thread_local! { static COUNTS: Cell<(bool, usize, usize)> = const { Cell::new((false,0,0)) }; }
fn record(n: usize) {
    let _ = COUNTS.try_with(|c| {
        let (on, a, b) = c.get();
        if on {
            c.set((on, a + 1, b + n));
        }
    });
}
unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        record(l.size());
        System.alloc(l)
    }
    unsafe fn alloc_zeroed(&self, l: Layout) -> *mut u8 {
        record(l.size());
        System.alloc_zeroed(l)
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
        System.dealloc(p, l);
    }
    unsafe fn realloc(&self, p: *mut u8, l: Layout, n: usize) -> *mut u8 {
        record(n);
        System.realloc(p, l, n)
    }
}
#[global_allocator]
static ALLOC: Counting = Counting;

#[derive(Clone, Copy, Default)]
#[repr(C)]
struct Token {
    kind: i32,
    start: i32,
    end: i32,
    size: i32,
    parent: i32,
}
extern "C" {
    fn bench_jsmn(data: *const u8, len: usize, tokens: *mut Token, cap: u32) -> i32;
    fn bench_token_size() -> usize;
}
fn tokenize(bytes: &[u8], tokens: &mut [Token]) -> Result<usize, ()> {
    let n = unsafe {
        bench_jsmn(
            bytes.as_ptr(),
            bytes.len(),
            tokens.as_mut_ptr(),
            tokens.len() as u32,
        )
    };
    if n < 0 {
        Err(())
    } else {
        Ok(n as usize)
    }
}
#[derive(Deserialize)]
struct Envelope<'a> {
    #[serde(rename = "type", borrow)]
    kind: Option<Cow<'a, str>>,
    #[serde(borrow)]
    id: Option<Cow<'a, str>>,
    #[serde(borrow)]
    stream: Option<Cow<'a, str>>,
    #[serde(borrow)]
    token: Option<Cow<'a, str>>,
    #[serde(borrow, rename = "params")]
    _params: Option<&'a RawValue>,
}
fn hash(s: &str) -> u64 {
    s.bytes()
        .fold(17u64, |v, b| v.wrapping_mul(31).wrapping_add(b as u64))
}
fn decoded(bytes: &[u8], t: Token) -> Cow<'_, str> {
    assert_eq!(t.kind, 4); // JSMN_STRING in the pinned header
    let part = &bytes[t.start as usize..t.end as usize];
    if part.contains(&b'\\') {
        Cow::Owned(
            serde_json::from_slice::<String>(&bytes[t.start as usize - 1..t.end as usize + 1])
                .unwrap(),
        )
    } else {
        Cow::Borrowed(std::str::from_utf8(part).unwrap())
    }
}
fn extract_jsmn(bytes: &[u8], tokens: &mut [Token]) -> u64 {
    let n = tokenize(bytes, tokens).unwrap();
    assert_eq!(tokens[0].kind, 1);
    let mut values = [0u64; 4];
    let mut i = 1;
    while i < n {
        let key = decoded(bytes, tokens[i]);
        let value = tokens[i + 1];
        if let Some(slot) = ["type", "id", "stream", "token"]
            .iter()
            .position(|k| *k == key)
        {
            values[slot] = hash(&decoded(bytes, value));
        }
        i += 2;
        while i < n && tokens[i].start < value.end {
            i += 1;
        }
    }
    values.into_iter().fold(0, u64::wrapping_add)
}
fn operation(mode: &str, bytes: &[u8], tokens: &mut [Token]) -> u64 {
    match mode {
        "serde_value" => {
            let v: Value = serde_json::from_slice(bytes).unwrap();
            ["type", "id", "stream", "token"]
                .iter()
                .filter_map(|k| v[*k].as_str())
                .map(hash)
                .fold(0, u64::wrapping_add)
        }
        "serde_borrowed" => {
            let v: Envelope = serde_json::from_slice(bytes).unwrap();
            [v.kind, v.id, v.stream, v.token]
                .iter()
                .flatten()
                .map(|v| hash(v))
                .fold(0, u64::wrapping_add)
        }
        "jsmn_extract" => extract_jsmn(bytes, tokens),
        "jsmn_tokens_only" => tokenize(bytes, tokens).unwrap() as u64,
        "raw_owned_copy" => {
            let copy = bytes.to_vec();
            black_box(&copy);
            copy.len() as u64
        }
        _ => panic!("unknown mode"),
    }
}
fn main() {
    assert_eq!(unsafe { bench_token_size() }, std::mem::size_of::<Token>());
    let mut fixtures=vec![
        ("ws_auth".to_owned(),br#"{"type":"auth","token":"eyJhbGciOiJSUzI1NiJ9.payload.signature"}"#.to_vec()),
        ("ws_subscribe".into(),br#"{"type":"subscribe","id":"s1","stream":"tasks","params":{}}"#.to_vec()),
        ("ws_escapes".into(),br#"{"ty\u0070e":"subscribe","id":"a\n\uD83D\uDE00","stream":"tasks","params":{"nested":[true,null,{"text":"hello"}]}}"#.to_vec()),
        ("ws_unsubscribe".into(),br#"{"type":"unsubscribe","id":"s1"}"#.to_vec()),
    ];
    for size in [1024, 16384, 65536, 1_048_576] {
        for shape in ["string", "numbers"] {
            let prefix = b"{\"type\":\"subscribe\",\"id\":\"s1\",\"stream\":\"tasks\",\"params\":";
            let mut bytes = prefix.to_vec();
            if shape == "string" {
                bytes.push(b'"');
                bytes.extend(std::iter::repeat_n(b'x', size - prefix.len() - 3));
                bytes.extend_from_slice(b"\"}");
            } else {
                bytes.push(b'[');
                while bytes.len() + 7 < size {
                    bytes.extend_from_slice(b"1.25,");
                }
                bytes.extend_from_slice(b"0]}");
            }
            fixtures.push((format!("{shape}_{size}"), bytes));
        }
    }
    for (name, bytes) in fixtures {
        let mut tokens = vec![Token::default(); bytes.len() / 2 + 16];
        let expected = operation("serde_value", &bytes, &mut tokens);
        assert_eq!(operation("serde_borrowed", &bytes, &mut tokens), expected);
        assert_eq!(operation("jsmn_extract", &bytes, &mut tokens), expected);
        for mode in [
            "serde_value",
            "serde_borrowed",
            "jsmn_extract",
            "jsmn_tokens_only",
            "raw_owned_copy",
        ] {
            for _ in 0..20 {
                black_box(operation(mode, &bytes, &mut tokens));
            }
            COUNTS.with(|c| c.set((true, 0, 0)));
            black_box(operation(mode, &bytes, &mut tokens));
            let (_, allocations, allocated) = COUNTS.with(|c| {
                let v = c.get();
                c.set((false, 0, 0));
                v
            });
            let mut ns = vec![];
            for _ in 0..5 {
                let start = Instant::now();
                let mut count = 0;
                while start.elapsed().as_millis() < 120 {
                    for _ in 0..16 {
                        black_box(operation(mode, black_box(&bytes), &mut tokens));
                    }
                    count += 16;
                }
                ns.push(start.elapsed().as_nanos() as f64 / count as f64);
            }
            println!(
                "{}",
                json!({"fixture":name,"mode":mode,"bytes":bytes.len(),"ns_per_operation":ns,
                "allocations":allocations,"allocated_bytes":allocated,
                "reused_scratch_bytes":if mode.starts_with("jsmn") {tokens.len()*std::mem::size_of::<Token>()}else{0},
                "task":if mode=="raw_owned_copy" {"copy_only"} else if mode=="jsmn_tokens_only" {"tokenize_only"} else {"extract_envelope_fields"}})
            );
        }
    }
    for bytes in [
        b"{\"x\":tru}".as_slice(),
        b"{\"x\":01}",
        b"{} {}",
        b"{\"x\":1,}",
        b"{\"x\":\"\\ud800\"}",
    ] {
        let mut tokens = vec![Token::default(); 128];
        println!(
            "{}",
            json!({"malformed":String::from_utf8_lossy(bytes),
            "serde_accepts":serde_json::from_slice::<Value>(bytes).is_ok(),"jsmn_accepts":tokenize(bytes,&mut tokens).is_ok()})
        );
    }
}
