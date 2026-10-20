#[path = "../src/input.rs"]
mod input;
#[path = "../src/output.rs"]
mod output;

use std::{
    alloc::{GlobalAlloc, Layout, System},
    cell::Cell,
    ffi::CString,
    hint::black_box,
};

struct Counting;
thread_local! {
    static COUNTS: Cell<(bool, usize, usize)> = const { Cell::new((false, 0, 0)) };
}
fn record(size: usize) {
    let _ = COUNTS.try_with(|counts| {
        let (enabled, calls, bytes) = counts.get();
        if enabled {
            counts.set((true, calls + 1, bytes + size));
        }
    });
}
unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        record(layout.size());
        System.alloc(layout)
    }
    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        record(layout.size());
        System.alloc_zeroed(layout)
    }
    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        System.dealloc(ptr, layout)
    }
    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, size: usize) -> *mut u8 {
        record(size);
        System.realloc(ptr, layout, size)
    }
}
#[global_allocator]
static ALLOCATOR: Counting = Counting;

#[test]
fn compare_input_allocations() {
    let schema = jsonschema::validator_for(&serde_json::json!({
        "type":"object", "required":["padding"], "additionalProperties":false,
        "properties":{"padding":{"type":"string"}}
    }))
    .unwrap();
    for size in [64, 1024, 16384, 65536] {
        let bytes = format!("{{\"padding\":\"{}\"}}", "x".repeat(size - 14)).into_bytes();
        assert_eq!(bytes.len(), size);
        for mode in ["before", "raw", "schema"] {
            // Compile/initialize everything before counting only this request path.
            COUNTS.with(|counts| counts.set((true, 0, 0)));
            let parameter = if mode == "before" {
                let mut copy = vec![0; 65536];
                copy[..size].copy_from_slice(&bytes);
                let value: serde_json::Value = serde_json::from_slice(&copy[..size]).unwrap();
                assert!(schema.is_valid(&value));
                let serialized = value.to_string();
                CString::new(serialized.as_str()).unwrap()
            } else {
                let parameter = unsafe {
                    input::copy_body(size as isize, |dst, cap| {
                        std::ptr::copy_nonoverlapping(bytes.as_ptr(), dst, cap);
                        cap as isize
                    })
                }
                .unwrap();
                if mode == "schema" {
                    input::check_schema(&parameter, &schema).unwrap();
                }
                parameter
            };
            black_box(&parameter);
            let (_, calls, allocated) = COUNTS.with(|counts| {
                let values = counts.get();
                counts.set((false, 0, 0));
                values
            });
            if mode == "raw" {
                assert_eq!(calls, 1, "raw input must use one allocation");
                assert_eq!(allocated, size + 1);
                assert_eq!(parameter.as_bytes(), bytes);
            }
            println!("PGXIO_INPUT_ALLOCATIONS {{\"mode\":\"{mode}\",\"body_bytes\":{size},\"allocations\":{calls},\"allocated_bytes\":{allocated}}}");
        }
    }
}

#[test]
fn borrowing_does_not_allocate_or_copy_body_bytes() {
    for size in [1024, 16384, 65536] {
        let bytes = vec![b'x'; size];
        COUNTS.with(|counts| counts.set((true, 0, 0)));
        let value = unsafe {
            input::Input::borrow(
                1,
                vec![input::Segment {
                    data: bytes.as_ptr(),
                    len: size,
                }],
            )
            .unwrap()
        };
        let (_, calls, allocated) = COUNTS.with(|counts| {
            let values = counts.get();
            counts.set((false, 0, 0));
            values
        });
        assert_eq!(calls, 1);
        assert_eq!(allocated, std::mem::size_of::<input::Segment>());
        assert_eq!(value.segments()[0].data, bytes.as_ptr());
        println!("PGXIO_INPUT_ALLOCATIONS {{\"mode\":\"borrowed\",\"body_bytes\":{size},\"allocations\":{calls},\"allocated_bytes\":{allocated},\"body_copies\":0}}");
    }
}

#[test]
fn raw_output_has_one_body_allocation_and_preserves_numeric_text() {
    for size in [1024, 16384, 65536, 1_048_576] {
        let bytes = format!("{{\"padding\":\"{}\"}}", "x".repeat(size - 14)).into_bytes();
        COUNTS.with(|counts| counts.set((true, 0, 0)));
        let value = output::capture(&bytes, None).unwrap();
        let (_, calls, allocated) = COUNTS.with(|c| {
            let v = c.get();
            c.set((false, 0, 0));
            v
        });
        assert_eq!((calls, allocated), (1, size));
        assert_eq!(value, bytes);
    }
    let bytes = br#"{ "number": 1844674407370955161700000000000000000000 }"#;
    assert_eq!(output::capture(bytes, None).unwrap(), bytes);
    assert!(output::capture(&vec![b' '; 1_048_577], None).is_err());
}
