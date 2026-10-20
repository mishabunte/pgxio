fn main() {
    if std::env::var_os("CARGO_FEATURE_BORROWED_LIBPQ").is_some() {
        let directory = std::env::var("PQ_LIB_DIR")
            .expect("borrowed-libpq requires PQ_LIB_DIR pointing to patched libpq");
        let output = std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
        let source = output.join("borrowed-probe.c");
        std::fs::write(&source, r#"
extern int PQsendQueryParamsBorrowed(void *, const char *, int, const unsigned *, const void *, int);
extern int PQsendQueryPreparedBorrowed(void *, const char *, int, const void *, int);
int main(void) {
    return PQsendQueryParamsBorrowed(0,0,0,0,0,0) + PQsendQueryPreparedBorrowed(0,0,0,0,0);
}
"#).unwrap();
        let status = cc::Build::new()
            .get_compiler()
            .to_command()
            .arg(&source)
            .arg("-L")
            .arg(&directory)
            .arg("-lpq")
            .arg("-o")
            .arg(output.join("borrowed-probe"))
            .status()
            .expect("run patched libpq link probe");
        assert!(
            status.success(),
            "borrowed-libpq requires both borrowed parameter symbols in patched libpq"
        );
        println!("cargo:rerun-if-env-changed=PQ_LIB_DIR");
    }
    let source =
        std::env::var("NGINX_SOURCE_DIR").expect("build with Docker, or set NGINX_SOURCE_DIR");
    let build = std::env::var("NGINX_BUILD_DIR").unwrap_or(format!("{source}/objs"));
    let mut c = cc::Build::new();
    c.file("native/adapter.c").include(build);
    for dir in [
        "src/core",
        "src/event",
        "src/event/modules",
        "src/os/unix",
        "src/http",
        "src/http/modules",
        "src/http/v2",
    ] {
        c.include(format!("{source}/{dir}"));
    }
    c.compile("pgxio_adapter");
    println!("cargo:rerun-if-changed=native/adapter.c");
    println!("cargo:rerun-if-env-changed=NGINX_SOURCE_DIR");
    println!("cargo:rerun-if-env-changed=NGINX_BUILD_DIR");
}
