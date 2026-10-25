//! Calls production input/borrowed code, linking a socket-free libpq fixture.
extern crate self as pq_sys;
pub type Oid = u32;
#[repr(C)]
pub struct PGconn { _private: [u8; 0] }
#[path = "../../../crates/pgxio-module/src/input.rs"]
mod input;
#[path = "../../../crates/pgxio-module/src/borrowed.rs"]
mod borrowed;
#[path = "../../../crates/pgxio-module/src/buffer.rs"]
mod buffer;

use input::{Input, Segment};
use std::{ffi::{c_char,c_int,c_void,CStr,CString}, hint::black_box, time::Instant};

unsafe extern "C" {
    fn synth_new(tls:c_int)->*mut c_void;
    fn synth_connection(f:*mut c_void)->*mut PGconn;
    fn synth_capture(f:*mut c_void);
    fn synth_output(f:*mut c_void,len:*mut usize)->*const u8;
    fn synth_complete(f:*mut c_void);
    fn synth_send(f:*mut c_void,borrow:c_int,body:*const c_char,parts:*const Segment,n:usize)->c_int;
    fn synth_free(f:*mut c_void);
    fn synth_cleanup();
    fn synth_cpu()->f64;
    fn synth_metrics_reset();
    fn synth_metric(index:c_int)->u64;
    fn synth_openssl()->*const c_char;
    fn PQsendQueryPrepared(conn:*mut PGconn,name:*const c_char,n:c_int,values:*const *const c_char,
                           lengths:*const c_int,formats:*const c_int,result:c_int)->c_int;
    fn dlsym(handle:*mut c_void,name:*const c_char)->*mut c_void;
}

struct Data { chunks:Vec<Vec<u8>>, segments:Vec<Segment>, contiguous:CString }
impl Data {
    fn new(size:usize,count:usize)->Self {
        let mut bytes=vec![b'x';size];
        bytes[..12].copy_from_slice(b"{\"padding\":\"");
        bytes[size-2..].copy_from_slice(b"\"}");
        for (i,b) in bytes[12..size-2].iter_mut().enumerate() { *b=b'a'+(i%26) as u8; }
        let chunks:Vec<_>=(0..count).map(|i|bytes[i*size/count..(i+1)*size/count].to_vec()).collect();
        let segments=chunks.iter().map(|c|Segment{data:c.as_ptr(),len:c.len()}).collect();
        Self { chunks,segments,contiguous:CString::new(bytes).unwrap() }
    }
    fn input(&self,borrow:bool)->Input {
        if borrow {
            // Descriptor construction and validation are included, payload setup is not.
            unsafe { Input::borrow(1,self.chunks.iter().map(|c|Segment{data:c.as_ptr(),len:c.len()}).collect()).unwrap() }
        } else {
            unsafe { input::copy_body(self.contiguous.as_bytes().len() as isize,|dst,cap| {
                let mut offset=0;
                for chunk in &self.chunks {
                    assert!(chunk.len()<=cap-offset);
                    std::ptr::copy_nonoverlapping(chunk.as_ptr(),dst.add(offset),chunk.len());
                    offset+=chunk.len();
                }
                offset as isize
            }).unwrap().into() }
        }
    }
}

unsafe fn operation(stage:&str,borrow:bool,data:&Data,fixture:*mut c_void,held:&mut Vec<Input>) {
    if stage=="input" {
        let value=data.input(borrow);
        black_box(&value);
        return;
    }
    if stage=="libpq" {
        assert_eq!(synth_send(fixture,borrow.into(),data.contiguous.as_ptr(),data.segments.as_ptr(),data.segments.len()),1);
        synth_complete(fixture);
        return;
    }
    let mut params=vec![data.input(borrow)];
    let conn=synth_connection(fixture);
    if borrow {
        assert!(borrowed::send(conn,c"",Some(c"bench"),&params));
        // Same ownership transfer as Connection::dispatch: no body may be dropped
        // before libpq reports a complete flush, even if submission flushed early.
        for param in &mut params { held.push(std::mem::replace(param,Input::Submitted)); }
    } else {
        let values:Vec<_>=params.iter().map(|p|p.owned().as_ptr()).collect();
        assert_eq!(PQsendQueryPrepared(conn,c"bench".as_ptr(),values.len() as c_int,values.as_ptr(),
                                       std::ptr::null(),std::ptr::null(),0),1);
    }
    synth_complete(fixture);
    held.clear();
    assert!(held.is_empty());
}

unsafe fn validate(stage:&str,data:&Data,tls:bool) {
    let mut previous=None;
    for borrow in [false,true] {
        if stage=="input" {
            let value=data.input(borrow);
            if borrow {
                let actual:Vec<_>=value.segments().iter().flat_map(|p|std::slice::from_raw_parts(p.data,p.len)).copied().collect();
                assert_eq!(actual,data.contiguous.as_bytes());
                assert!(value.segments().iter().zip(&data.chunks).all(|(s,c)|s.data==c.as_ptr()));
            } else { assert_eq!(value.owned().as_bytes(),data.contiguous.as_bytes()); }
        } else {
            let fixture=synth_new(tls.into());
            synth_capture(fixture);
            operation(stage,borrow,data,fixture,&mut Vec::new());
            let mut length=0;
            let raw=synth_output(fixture,&mut length);
            let actual=std::slice::from_raw_parts(raw,length).to_vec();
            // Independently inspect the Bind argument, not just agreement of modes.
            assert_eq!(actual[0],b'B');
            let bind_len=u32::from_be_bytes(actual[1..5].try_into().unwrap()) as usize;
            let mut at=5;
            assert_eq!(actual[at],0); at+=1; // unnamed portal
            assert_eq!(&actual[at..at+6],b"bench\0"); at+=6;
            let nformats=u16::from_be_bytes(actual[at..at+2].try_into().unwrap()) as usize;
            assert!(nformats<=1);
            assert!(actual[at+2..at+2+2*nformats].iter().all(|b|*b==0));
            at+=2+2*nformats;
            assert_eq!(u16::from_be_bytes(actual[at..at+2].try_into().unwrap()),1); at+=2;
            let n=u32::from_be_bytes(actual[at..at+4].try_into().unwrap()) as usize; at+=4;
            assert_eq!(&actual[at..at+n],data.contiguous.as_bytes());
            assert_eq!(actual[1+bind_len],b'D');
            // Default text format (copy) and explicit text format (borrowed)
            // have equivalent arguments but different Bind header lengths.
            let suffix=actual[1+bind_len..].to_vec();
            if let Some(expected)=&previous { assert_eq!(&suffix,expected); }
            previous=Some(suffix);
            synth_free(fixture);
        }
    }
}

unsafe fn symbol<T:Copy>(name:&CStr)->T {
    let address=dlsym(std::ptr::null_mut(),name.as_ptr());
    assert!(!address.is_null(),"diagnostics require allocation preload library");
    assert_eq!(std::mem::size_of::<T>(),std::mem::size_of::<*mut c_void>());
    std::mem::transmute_copy(&address)
}

fn main() { unsafe {
    let args:Vec<_>=std::env::args().collect();
    assert_eq!(args.len(),8,"stage mode transport size segments warmup duration");
    let (stage,mode,transport)=(&*args[1],&*args[2],&*args[3]);
    assert!(["input","libpq","combined"].contains(&stage));
    assert!(["copy","borrowed","auto"].contains(&mode));
    assert!(["plain","tls"].contains(&transport));
    let size:usize=args[4].parse().unwrap(); let count:usize=args[5].parse().unwrap();
    assert!((256..=65536).contains(&size) && (1..=16).contains(&count));
    let warmup:f64=args[6].parse().unwrap(); let duration:f64=args[7].parse().unwrap();
    assert!(warmup>=0. && duration>0.);
    let data=Data::new(size,count);
    let tls=transport=="tls";
    let mode_policy=match mode { "copy"=>buffer::BufferMode::Copy,"borrowed"=>buffer::BufferMode::Borrowed,_=>buffer::BufferMode::Auto };
    let selected=mode_policy.select(size,count,true,false,!tls || stage=="input");
    validate(stage,&data,tls);
    let fixture=if stage=="input" { std::ptr::null_mut() } else { synth_new(tls.into()) };
    let mut held=Vec::new();
    let warm=Instant::now();
    while warm.elapsed().as_secs_f64()<warmup {
        for _ in 0..64 { operation(stage,selected,&data,fixture,&mut held); }
    }
    synth_metrics_reset();
    let mut allocation_calls=0;
    let mut allocation_bytes=0;
    let mut operations:u64=0;
    let wall=Instant::now(); let cpu=synth_cpu();
    if cfg!(feature="diagnostics") {
        let begin:unsafe extern "C" fn()=symbol(c"synth_alloc_begin");
        let end:unsafe extern "C" fn()=symbol(c"synth_alloc_end");
        let calls:unsafe extern "C" fn()->u64=symbol(c"synth_alloc_calls");
        let bytes:unsafe extern "C" fn()->u64=symbol(c"synth_alloc_bytes");
        begin();
        for _ in 0..1000 { operation(stage,selected,&data,fixture,&mut held);operations+=1; }
        end(); allocation_calls=calls(); allocation_bytes=bytes();
    } else {
        while wall.elapsed().as_secs_f64()<duration {
            for _ in 0..64 { operation(stage,selected,&data,fixture,&mut held);operations+=1; }
        }
    }
    let cpu_seconds=synth_cpu()-cpu; let wall_seconds=wall.elapsed().as_secs_f64();
    black_box(operations);
    let diagnostics=cfg!(feature="diagnostics");
    let result=serde_json::json!({"kind":"borrowed_synthetic", "stage":stage,"mode":mode,
        "transport":if stage=="input" {"none"} else {transport},"body_bytes":size,"segments":count,
        "effective_mode":if selected {"borrowed"} else {"copy"},
        "operations":operations,"diagnostics":diagnostics,"validated":true,"socket_operations":0,
        "cpu_ns_per_op":if diagnostics {None}else{Some(cpu_seconds*1e9/operations as f64)},
        "wall_ns_per_op":if diagnostics {None}else{Some(wall_seconds*1e9/operations as f64)},
        "cpu_ns_per_byte":if diagnostics {None}else{Some(cpu_seconds*1e9/(operations as f64*size as f64))},
        "allocation_calls_per_op":if diagnostics {Some(allocation_calls as f64/operations as f64)}else{None},
        "allocated_bytes_per_op":if diagnostics {Some(allocation_bytes as f64/operations as f64)}else{None},
        "sink_calls_per_op":if diagnostics {Some(synth_metric(0) as f64/operations as f64)}else{None},
        "tls_write_calls_per_op":if diagnostics {Some(synth_metric(1) as f64/operations as f64)}else{None},
        "tls_records_per_op":if diagnostics {Some(synth_metric(2) as f64/operations as f64)}else{None},
        "ciphertext_bytes_per_op":if diagnostics {Some(synth_metric(3) as f64/operations as f64)}else{None},
        "openssl":CStr::from_ptr(synth_openssl()).to_str().unwrap(),
        "tls_cipher":if tls && stage!="input" {Some("TLS_AES_128_GCM_SHA256")}else{None}});
    if !fixture.is_null() { synth_free(fixture); }
    synth_cleanup();
    println!("{}",result);
} }
