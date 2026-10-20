#[path = "../src/auth.rs"]
mod auth;
#[path = "../src/buffer.rs"]
mod buffer;
#[path = "../src/config.rs"]
mod config;
#[path = "../src/contract.rs"]
mod contract;
#[path = "../src/input.rs"]
mod input;

fn body(bytes: &[u8]) -> Result<std::ffi::CString, input::Error> {
    unsafe {
        input::copy_body(bytes.len() as isize, |dst, cap| {
            assert_eq!(cap, bytes.len());
            std::ptr::copy_nonoverlapping(bytes.as_ptr(), dst, cap);
            cap as isize
        })
    }
}

#[test]
fn borrowed_input_retains_segment_pointers_and_checks_bytes() {
    let a = b"{\"title\":";
    let b = b"\"hello\"}";
    let parts = vec![
        input::Segment {
            data: a.as_ptr(),
            len: a.len(),
        },
        input::Segment {
            data: b.as_ptr(),
            len: b.len(),
        },
    ];
    let value = unsafe { input::Input::borrow(42, parts).unwrap() };
    assert_eq!(value.segments()[0].data, a.as_ptr());
    assert_eq!(value.segments()[1].data, b.as_ptr());
    assert!(value.borrowed());
    for (data, len, expected) in [
        (std::ptr::null(), 1, input::Error::InvalidJson),
        (a.as_ptr(), 65537, input::Error::TooLarge),
        (b"x\0".as_ptr(), 2, input::Error::InvalidJson),
    ] {
        assert!(
            matches!(unsafe { input::Input::borrow(1, vec![input::Segment {data,len}]) }, Err(e) if e == expected)
        );
    }
    assert!(unsafe {
        input::Input::borrow(
            1,
            vec![input::Segment {
                data: std::ptr::null(),
                len: 0,
            }],
        )
    }
    .is_ok());
}

#[test]
fn raw_input_preserves_bytes_without_parsing_or_normalizing() {
    for bytes in [
        &b" { \"z\":18446744073709551617, \"a\":\"\\u0061\" } \n"[..],
        &b"{malformed JSON"[..],
        &b"\xff"[..],
        &b""[..],
    ] {
        assert_eq!(body(bytes).unwrap().as_bytes(), bytes);
    }
    assert_eq!(body(b"{}\0junk"), Err(input::Error::InvalidJson));
    assert_eq!(body(&vec![b' '; 65536]).unwrap().as_bytes().len(), 65536);
    assert_eq!(body(&vec![b' '; 65537]), Err(input::Error::TooLarge));
    unsafe {
        assert_eq!(
            input::copy_body(-1, |_, _| panic!()),
            Err(input::Error::InvalidJson)
        );
        assert_eq!(
            input::copy_body(-2, |_, _| panic!()),
            Err(input::Error::TooLarge)
        );
        assert_eq!(
            input::copy_body(3, |_, _| -1),
            Err(input::Error::InvalidJson)
        );
    }
}

#[test]
fn optional_schema_checks_do_not_rewrite_the_original_bytes() {
    let schema = jsonschema::validator_for(&serde_json::json!({
        "type":"object", "required":["title"], "additionalProperties":false,
        "properties":{"title":{"type":"string","minLength":1}}
    }))
    .unwrap();
    let original = b" {\"title\": \"\\u0061\"} ";
    let bytes = body(original).unwrap();
    assert!(input::check_schema(&bytes, &schema).is_ok());
    assert_eq!(bytes.as_bytes(), original);
    assert_eq!(
        input::check_schema(&body(b"{}").unwrap(), &schema),
        Err(input::Error::InvalidInput)
    );
    assert_eq!(
        input::check_schema(&body(b"{bad}").unwrap(), &schema),
        Err(input::Error::InvalidJson)
    );
}

#[test]
fn rest_schema_checks_are_opt_in() {
    let base = serde_json::json!({"contract":"unused","issuer":"test","audience":"test"});
    let default: config::Config = serde_json::from_value(base.clone()).unwrap();
    assert!(!default.validate_rest_input);
    assert_eq!(default.libpq_buf, buffer::BufferMode::Auto);
    let strict: config::Config = serde_json::from_value(serde_json::json!({
        "validate_rest_input":true, "contract":"unused","issuer":"test","audience":"test"
    }))
    .unwrap();
    assert!(strict.validate_rest_input);
}

#[test]
fn borrowed_configuration_requires_the_experimental_build() {
    let c: config::Config = serde_json::from_value(serde_json::json!({
        "contract":"unused", "issuer":"test", "audience":"test", "libpq_buf":"borrowed"
    }))
    .unwrap();
    let result = c.validate();
    if cfg!(feature = "borrowed-libpq") {
        assert_eq!(result.unwrap().libpq_buf, buffer::BufferMode::Borrowed);
    } else {
        assert!(matches!(result, Err(message) if message.contains("experimental borrowed-libpq")));
    }
}

#[test]
fn buffer_policy_boundaries_and_capabilities() {
    use buffer::BufferMode::*;
    for mode in [Copy, Borrowed, Auto] {
        assert!(!mode.select(65536, 1, false, false, true));
        assert!(!mode.select(65536, 1, true, true, true));
        assert!(!mode.select(0, 0, true, false, true));
        assert!(!mode.select(65536, input::MAX_SEGMENTS + 1, true, false, true));
    }
    assert!(!Copy.select(65536, 1, true, false, true));
    assert!(Borrowed.select(1024, 16, true, false, false));
    assert!(!Auto.select(16383, 1, true, false, true));
    assert!(Auto.select(16384, 1, true, false, true));
    assert!(Auto.select(16384, 4, true, false, true));
    assert!(!Auto.select(16384, 5, true, false, true));
    assert!(Auto.select(65536, 16, true, false, true));
    assert!(!Auto.select(65536, 17, true, false, true));
    assert!(!Auto.select(65536, 1, true, false, false));
}

#[test]
fn transport_cache_disconnect_reconnect_and_full_pool() {
    let mut pool = buffer::PlaintextPool::default();
    assert!(!pool.all_plaintext(0));
    assert!(!pool.all_plaintext(2));
    pool.update(0, true);
    assert!(!pool.all_plaintext(2));
    pool.update(1, true);
    assert!(pool.all_plaintext(2));
    pool.update(0, false); // Unknown, disconnect, TLS or GSS.
    assert!(!pool.all_plaintext(2));
    pool.update(0, true);
    assert!(pool.all_plaintext(2));
    for id in 0..64 { pool.update(id, true); }
    assert!(pool.all_plaintext(64));
    pool.update(63, false);
    assert!(!pool.all_plaintext(64));
}

#[test]
fn queued_auto_candidate_can_be_copied_without_losing_bytes() {
    let bytes = b"{\"x\":1}";
    let value = unsafe { input::Input::borrow(1, vec![
        input::Segment { data: bytes[..3].as_ptr(), len: 3 },
        input::Segment { data: std::ptr::null(), len: 0 },
        input::Segment { data: bytes[3..].as_ptr(), len: bytes.len()-3 },
    ]).unwrap() };
    let owned = value.into_owned_copy().unwrap();
    assert!(!owned.borrowed());
    assert_eq!(owned.owned().as_bytes(), bytes);
}

#[test]
fn buffer_directive_is_typed_and_the_old_directive_is_rejected() {
    let base = "pgxio_issuer test; pgxio_audience test; pgxio_endpoint x web.x;";
    for mode in ["copy", "auto"] {
        assert!(config::Settings::parse(&directives(&format!("{base} pgxio_libpq_buf {mode};"))).is_ok());
    }
    for suffix in ["pgxio_libpq_buf invalid;", "pgxio_libpq_buf on;",
        "pgxio_libpq_buf auto; pgxio_libpq_buf copy;", "pgxio_db_borrowed_buffers on;"] {
        assert!(config::Settings::parse(&directives(&format!("{base} {suffix}"))).is_err());
    }
}

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
    c.streams[0].name = "invalid.name".into();
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
    let c = serde_json::from_slice::<config::Config>(&std::fs::read(&root).unwrap())
        .map_err(|e| e.to_string())
        .and_then(config::Config::validate)
        .unwrap();
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
        assert!(
            serde_json::from_slice::<config::Config>(&std::fs::read(&root).unwrap())
                .map_err(|e| e.to_string())
                .and_then(config::Config::validate)
                .is_err()
        );
        value.as_object_mut().unwrap().remove(key);
    }
    value["jwt_cache_max_entries"] = serde_json::json!(1);
    value["jwt_cache_max_bytes"] = serde_json::json!(0);
    std::fs::write(&root, value.to_string()).unwrap();
    assert!(
        serde_json::from_slice::<config::Config>(&std::fs::read(&root).unwrap())
            .map_err(|e| e.to_string())
            .and_then(config::Config::validate)
            .is_err()
    );
    std::fs::remove_file(root).unwrap();
}

fn directives(text: &str) -> Vec<(String, Vec<String>)> {
    text.split(';')
        .filter_map(|s| {
            let mut words = s.split_whitespace();
            words
                .next()
                .map(|name| (name.into(), words.map(str::to_owned).collect()))
        })
        .collect()
}

#[test]
fn native_routing_is_independent_of_schemas() {
    let base = "pgxio_issuer test; pgxio_audience test; pgxio_endpoint hello web.hello read_only; pgxio_stream changes web.authorize;";
    let s = config::Settings::parse(&directives(base)).unwrap();
    assert!(s.config.contract.is_none());
    assert!(!s.config.validate_output);
    assert!(s.routes.endpoints[0].read_only);
    assert_eq!(
        s.routes.openapi()["paths"]["/v1/hello"]["post"]["responses"]["200"]["content"]
            ["application/json"]["schema"],
        serde_json::json!({})
    );
    for suffix in [
        "pgxio_validate_output on;",
        "pgxio_validate_input on;",
        "pgxio_endpoint hello web.other;",
        "pgxio_pool_size 0;",
        "pgxio_issuer duplicate;",
        "pgxio_endpoint other private.secret;",
        "pgxio_validate_output true;",
        "pgxio_stream changes web.duplicate;",
        "pgxio_pool_queue_limit -1;",
    ] {
        assert!(
            config::Settings::parse(&directives(&format!("{base}{suffix}"))).is_err(),
            "{suffix}"
        );
    }
}

#[test]
fn optional_contract_coverage_and_metadata_do_not_enable_checks() {
    let base = "pgxio_issuer test; pgxio_audience test; pgxio_endpoint list_tasks web.list_tasks read_only; pgxio_endpoint create_task web.create_task; pgxio_stream tasks web.authorize_tasks; pgxio_contract ../../examples/contract.json;";
    let s = config::Settings::parse(&directives(base)).unwrap();
    assert!(!s.config.validate_rest_input && !s.config.validate_output);
    assert_eq!(s.routes.endpoints[1].input["required"][0], "title");
    assert!(config::Settings::parse(&directives(&format!(
        "{base}pgxio_validate_input on; pgxio_validate_output on;"
    )))
    .is_ok());
    assert!(config::Settings::parse(&directives(
        &base.replace("pgxio_endpoint create_task web.create_task;", "")
    ))
    .is_err());
    assert!(config::Settings::parse(&directives(&format!(
        "{base}pgxio_endpoint uncovered web.uncovered; pgxio_validate_output on;"
    )))
    .is_err());
    assert!(serde_json::from_value::<contract::Contract>(serde_json::json!({"version":2,"endpoints":[{"name":"hello","function":"web.hello","input":{},"output":{}}]})).is_err());
}
