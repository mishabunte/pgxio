fn main() {
    cc::Build::new()
        .file("wrapper.c")
        .include("vendor")
        .opt_level(3)
        .compile("jsmn_bench");
    println!("cargo:rerun-if-changed=wrapper.c");
    println!("cargo:rerun-if-changed=vendor/jsmn.h");
}
