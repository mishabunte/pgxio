use crate::contract::{identifier, Contract, Endpoint, Stream};
use crate::buffer::BufferMode;
use serde::Deserialize;
use serde_json::json;
use std::collections::HashSet;

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    #[serde(default)]
    pub libpq_buf: BufferMode,
    #[serde(default)]
    pub validate_rest_input: bool,
    #[cfg(feature = "benchmark-auth")]
    #[serde(default)]
    pub benchmark_no_jwt: bool,
    #[serde(default = "enabled")]
    pub db_prepared_statements: bool,
    #[serde(default = "enabled")]
    pub db_pipeline: bool,
    #[serde(default = "wait")]
    pub pool_acquisition_timeout_ms: u64,
    #[serde(default = "queue")]
    pub pool_queue_limit: usize,
    #[serde(default)]
    pub jwt_cache_max_entries: usize,
    #[serde(default = "cache_bytes")]
    pub jwt_cache_max_bytes: usize,
    #[serde(default)]
    pub contract: Option<String>,
    #[serde(default)]
    pub validate_output: bool,
    pub issuer: String,
    pub audience: String,
    pub jwks_file: Option<String>,
    #[serde(default)]
    pub development: bool,
    #[serde(default)]
    pub origins: Vec<String>,
    #[serde(default = "pool")]
    pub pool_size: usize,
    #[serde(default = "sockets")]
    pub max_websockets: usize,
}
fn enabled() -> bool {
    true
}
fn wait() -> u64 {
    1000
}
fn queue() -> usize {
    32
}
fn cache_bytes() -> usize {
    1_048_576
}
fn pool() -> usize {
    4
}
fn sockets() -> usize {
    1024
}
impl Config {
    pub fn benchmark_bypass(&self, name: &str, function: &str) -> bool {
        #[cfg(feature = "benchmark-auth")]
        {
            self.benchmark_no_jwt
                && matches!(
                    name,
                    "bench_public_one" | "bench_public_page" | "bench_public_write"
                )
                && function == format!("web.{name}")
        }
        #[cfg(not(feature = "benchmark-auth"))]
        {
            let _ = (name, function);
            false
        }
    }

    pub fn validate(self) -> Result<Self, String> {
        let c = self;
        if c.issuer.is_empty() || c.audience.is_empty() {
            return Err("pgxio_issuer and pgxio_audience are required".into());
        }
        if c.libpq_buf == BufferMode::Borrowed && !cfg!(feature = "borrowed-libpq") {
            return Err(
                "pgxio_libpq_buf borrowed requires the experimental borrowed-libpq build".into(),
            );
        }
        if c.pool_acquisition_timeout_ms > 60_000
            || c.pool_queue_limit > 4096
            || c.jwt_cache_max_entries > 100_000
            || c.jwt_cache_max_bytes > 67_108_864
            || (c.jwt_cache_max_entries > 0 && c.jwt_cache_max_bytes == 0)
        {
            return Err("invalid admission or cache limits".into());
        }
        if !(1..=64).contains(&c.pool_size) || !(1..=100_000).contains(&c.max_websockets) {
            return Err("invalid connection limits".into());
        }
        if c.origins
            .iter()
            .any(|s| s == "*" || s.contains(['\r', '\n']))
        {
            return Err("origins must be exact, without wildcards".into());
        }
        Ok(c)
    }
}

/// Owned snapshot built at Nginx configuration time, cloned by each worker.
#[derive(Clone)]
pub struct Settings {
    pub config: Config,
    pub routes: Contract,
}

impl Settings {
    pub fn parse(directives: &[(String, Vec<String>)]) -> Result<Self, String> {
        let mut config = serde_json::Map::new();
        let mut routes = Contract {
            version: 2,
            endpoints: vec![],
            streams: vec![],
        };
        let mut origins = vec![];
        let mut seen = HashSet::new();
        for (name, args) in directives {
            let key = name
                .strip_prefix("pgxio_")
                .ok_or("invalid pgxio directive")?;
            match key {
                "endpoint" => {
                    if !(2..=3).contains(&args.len()) || (args.len() == 3 && args[2] != "read_only")
                    {
                        return Err("pgxio_endpoint expects name web.function [read_only]".into());
                    }
                    routes.endpoints.push(Endpoint {
                        name: args[0].clone(),
                        function: args[1].clone(),
                        read_only: args.len() == 3,
                        input: json!({}),
                        output: json!({}),
                    });
                }
                "stream" => {
                    if args.len() != 2 {
                        return Err("pgxio_stream expects name web.authorize".into());
                    }
                    routes.streams.push(Stream {
                        name: args[0].clone(),
                        authorize: args[1].clone(),
                        input: json!({}),
                    });
                }
                "origin" => {
                    if args.len() != 1 {
                        return Err("pgxio_origin expects one origin".into());
                    }
                    origins.push(args[0].clone());
                }
                _ => {
                    if args.len() != 1 || !seen.insert(key) {
                        return Err(format!("invalid or duplicate {name}"));
                    }
                    let value = match key {
                        "validate_input"
                        | "validate_output"
                        | "benchmark_no_jwt"
                        | "db_prepared_statements"
                        | "db_pipeline"
                        | "development" => match args[0].as_str() {
                            "on" => json!(true),
                            "off" => json!(false),
                            _ => return Err(format!("{name} expects on or off")),
                        },
                        "pool_acquisition_timeout_ms"
                        | "pool_queue_limit"
                        | "jwt_cache_max_entries"
                        | "jwt_cache_max_bytes"
                        | "pool_size"
                        | "max_websockets" => {
                            json!(args[0]
                                .parse::<u64>()
                                .map_err(|_| format!("{name} expects an unsigned integer"))?)
                        }
                        "libpq_buf" | "contract" | "issuer" | "audience" | "jwks_file" => json!(args[0]),
                        _ => return Err(format!("unknown directive {name}")),
                    };
                    config.insert(
                        if key == "validate_input" {
                            "validate_rest_input"
                        } else {
                            key
                        }
                        .into(),
                        value,
                    );
                }
            }
        }
        config.insert("origins".into(), json!(origins));
        let config: Config = serde_json::from_value(config.into()).map_err(|e| e.to_string())?;
        let config = config.validate()?;
        let mut names = HashSet::new();
        for e in &routes.endpoints {
            if !identifier(&e.name)
                || !names.insert(&e.name)
                || !e.function.strip_prefix("web.").is_some_and(identifier)
            {
                return Err("invalid or duplicate endpoint declaration".into());
            }
        }
        names.clear();
        for s in &routes.streams {
            if !identifier(&s.name)
                || !names.insert(&s.name)
                || !s.authorize.strip_prefix("web.").is_some_and(identifier)
            {
                return Err("invalid or duplicate stream declaration".into());
            }
        }
        let schema = config
            .contract
            .as_deref()
            .map(Contract::load)
            .transpose()
            .map_err(|e| e.to_string())?;
        if let Some(schema) = &schema {
            for e in &schema.endpoints {
                let route = routes
                    .endpoints
                    .iter_mut()
                    .find(|r| r.name == e.name)
                    .ok_or("contract contains an undeclared endpoint")?;
                route.input = e.input.clone();
                route.output = e.output.clone();
            }
            for s in &schema.streams {
                let route = routes
                    .streams
                    .iter_mut()
                    .find(|r| r.name == s.name)
                    .ok_or("contract contains an undeclared stream")?;
                route.input = s.input.clone();
            }
        }
        if config.validate_rest_input || config.validate_output {
            let schema = schema
                .as_ref()
                .ok_or("schema validation requires pgxio_contract")?;
            if routes
                .endpoints
                .iter()
                .any(|r| !schema.endpoints.iter().any(|e| e.name == r.name))
                || (config.validate_rest_input
                    && routes
                        .streams
                        .iter()
                        .any(|r| !schema.streams.iter().any(|s| s.name == r.name)))
            {
                return Err("schema validation requires complete contract coverage".into());
            }
        }
        Ok(Self { config, routes })
    }
}
