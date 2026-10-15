#[path = "../src/auth.rs"]
mod auth;
#[path = "../src/config.rs"]
mod config;
#[path = "../src/contract.rs"]
mod contract;

#[test]
fn benchmark_auth_is_explicit_and_cannot_bypass_protected_functions() {
    let value = serde_json::json!({"contract":"unused", "issuer":"test", "audience":"test",
        "benchmark_no_jwt":true});
    #[cfg(not(feature = "benchmark-auth"))]
    assert!(serde_json::from_value::<config::Config>(value).is_err());
    #[cfg(feature = "benchmark-auth")]
    {
        let mut c: config::Config = serde_json::from_value(value).unwrap();
        assert!(c.benchmark_bypass("bench_public_page", "web.bench_public_page"));
        assert!(!c.benchmark_bypass("bench_page", "web.bench_page"));
        assert!(!c.benchmark_bypass("bench_public_page", "web.bench_page"));
        c.benchmark_no_jwt = false;
        assert!(!c.benchmark_bypass("bench_public_page", "web.bench_public_page"));
    }
    let normal: config::Config = serde_json::from_value(serde_json::json!({
        "contract":"unused", "issuer":"test", "audience":"test"}))
    .unwrap();
    assert!(!normal.benchmark_bypass("bench_public_page", "web.bench_public_page"));
}

#[test]
fn validates_example_contract_and_streams() {
    let mut c = contract::Contract::load("../../examples/contract.json").unwrap();
    assert!(c.openapi()["paths"]["/v1/list_tasks"].is_object());
    c.streams[0].authorize = "private.read_all".into();
    assert!(c.validate().is_err());
}

#[test]
fn authentication_rejects_wrong_audience_expiry_and_signature() {
    use jsonwebtoken::{encode, EncodingKey, Header};
    use serde_json::json;
    let secret = "test-secret-at-least-thirty-two-characters";
    std::env::set_var("PGXIO_DEV_SECRET", secret);
    let a = auth::Auth::new("test".into(), "pgxio".into(), None, true).unwrap();
    let exp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs()
        + 60;
    let mut claims = json!({"sub":"alice","iss":"test","aud":"pgxio","exp":exp});
    let sign = |c: &serde_json::Value, s: &str| {
        encode(
            &Header::default(),
            c,
            &EncodingKey::from_secret(s.as_bytes()),
        )
        .unwrap()
    };
    assert!(a.verify(&sign(&claims, secret)).is_ok());
    assert!(a.verify(&sign(&claims, "wrong")).is_err());
    claims["aud"] = json!("another-app");
    assert!(a.verify(&sign(&claims, secret)).is_err());
    claims["aud"] = json!("pgxio");
    claims["exp"] = json!(1);
    assert!(a.verify(&sign(&claims, secret)).is_err());
    assert!(auth::Auth::new("test".into(), "pgxio".into(), None, false).is_err());
}

#[test]
fn performance_defaults_and_limits_are_explicit() {
    let root = std::env::temp_dir().join(format!("pgxio-config-test-{}", std::process::id()));
    let mut value = serde_json::json!({"contract":"unused","issuer":"test","audience":"test"});
    std::fs::write(&root, value.to_string()).unwrap();
    let c = config::Config::load(root.to_str().unwrap()).unwrap();
    assert!(c.db_pipeline && c.db_prepared_statements);
    assert_eq!(c.pool_acquisition_timeout_ms, 1000);
    assert_eq!(c.pool_queue_limit, 32);
    assert_eq!(c.jwt_cache_max_entries, 0);
    for (key, invalid) in [
        ("pool_queue_limit", 4097),
        ("pool_acquisition_timeout_ms", 60001),
        ("jwt_cache_max_entries", 100001),
        ("jwt_cache_max_bytes", 67108865),
    ] {
        value[key] = serde_json::json!(invalid);
        std::fs::write(&root, value.to_string()).unwrap();
        assert!(config::Config::load(root.to_str().unwrap()).is_err());
        value.as_object_mut().unwrap().remove(key);
    }
    value["jwt_cache_max_entries"] = serde_json::json!(1);
    value["jwt_cache_max_bytes"] = serde_json::json!(0);
    std::fs::write(&root, value.to_string()).unwrap();
    assert!(config::Config::load(root.to_str().unwrap()).is_err());
    std::fs::remove_file(root).unwrap();
}
