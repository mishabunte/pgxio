fn main() {
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
