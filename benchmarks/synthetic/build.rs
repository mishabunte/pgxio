use std::{env, process::Command};

fn main() {
    let out = env::var("OUT_DIR").unwrap();
    let mut cc = Command::new("cc");
    cc.args(["-O2", "-g", "-fPIC", "-I/postgres/src/include", "-I/postgres/src/interfaces/libpq", "-I/opt/pgxio-libpq/include"]);
    if env::var_os("CARGO_FEATURE_DIAGNOSTICS").is_some() {
        cc.arg("-DSYNTH_DIAGNOSTICS=1");
    }
    cc.args(["-c", "fixture.c", "-o", &format!("{out}/fixture.o")]);
    assert!(cc.status().unwrap().success());
    assert!(Command::new("ar").args(["crs", &format!("{out}/libfixture.a"), &format!("{out}/fixture.o")]).status().unwrap().success());
    println!("cargo:rustc-link-search=native={out}");
    println!("cargo:rustc-link-lib=static=fixture");
    println!("cargo:rustc-link-search=native=/opt/pgxio-libpq/lib");
    println!("cargo:rustc-link-lib=static=pq");
    println!("cargo:rustc-link-search=native=/postgres/src/common");
    println!("cargo:rustc-link-search=native=/postgres/src/port");
    println!("cargo:rustc-link-lib=static=pgcommon_shlib");
    println!("cargo:rustc-link-lib=static=pgport_shlib");
    for lib in ["ssl", "crypto", "m", "dl", "pthread"] {
        println!("cargo:rustc-link-lib={lib}");
    }
    for symbol in ["send", "sendmsg", "socket", "socketpair", "connect", "recv", "recvfrom", "recvmsg"] {
        println!("cargo:rustc-link-arg=-Wl,--wrap={symbol}");
    }
    if env::var_os("CARGO_FEATURE_DIAGNOSTICS").is_some() {
        println!("cargo:rustc-link-arg=-Wl,--wrap=SSL_write");
    }
    println!("cargo:rerun-if-changed=fixture.c");
}
