use serde::Deserialize;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
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
    pub contract: String,
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

    pub fn load(path: &str) -> Result<Self, String> {
        let c: Self = serde_json::from_slice(&std::fs::read(path).map_err(|e| e.to_string())?)
            .map_err(|e| e.to_string())?;
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
