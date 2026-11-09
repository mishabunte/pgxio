mod auth;
mod contract;

use axum::{body::{to_bytes, Body}, extract::{Path, Request, State}, http::{header, HeaderValue, Method, StatusCode}, response::{IntoResponse, Response}, routing::{get, post}, Json, Router};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod, Runtime};
use serde_json::{json, Value};
use std::{collections::HashMap, env, sync::Arc, time::Duration};
use tokio::sync::Semaphore;
use tower_http::cors::CorsLayer;

type Error = Box<dyn std::error::Error + Send + Sync>;
struct Endpoint {
    spec: contract::Endpoint,
    input: jsonschema::Validator,
    output: jsonschema::Validator,
}
struct App {
    pool: Pool,
    auth: auth::Auth,
    endpoints: HashMap<String, Endpoint>,
    openapi: Value,
    admission: Semaphore,
    timeout: Duration,
    body_limit: usize,
    response_limit: usize,
}

#[derive(Debug)]
struct ApiError(StatusCode, &'static str, &'static str);
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let mut response = (self.0, Json(json!({"error":{"code":self.1,"message":self.2}}))).into_response();
        response.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
        if self.0 == StatusCode::UNAUTHORIZED {
            response.headers_mut().insert(header::WWW_AUTHENTICATE, HeaderValue::from_static("Bearer"));
        }
        response
    }
}
fn unavailable() -> ApiError { ApiError(StatusCode::SERVICE_UNAVAILABLE, "unavailable", "Database or request capacity unavailable") }
fn db_error(e: tokio_postgres::Error) -> ApiError {
    match e.code().map(|c| c.code()) {
        Some("42501") => ApiError(StatusCode::FORBIDDEN,"forbidden","Operation not permitted"),
        Some("23505" | "23503" | "23514") => ApiError(StatusCode::CONFLICT,"conflict","Operation conflicts with database constraints"),
        Some("22023" | "22P02") => ApiError(StatusCode::BAD_REQUEST,"invalid_input","Invalid input"),
        Some("57014") => ApiError(StatusCode::GATEWAY_TIMEOUT,"query_timeout","Query deadline exceeded"),
        _ => unavailable(),
    }
}
fn integer(name: &str, default: usize, max: usize) -> Result<usize, Error> {
    let n = env::var(name).map(|s| s.parse()).unwrap_or(Ok(default))?;
    if n == 0 || n > max { return Err(format!("{name} must be between 1 and {max}").into()); }
    Ok(n)
}

async fn pool() -> Result<Pool, Error> {
    let uri = env::var("PGXIO_DATABASE_URL")?;
    // tokio-postgres calls its enforced TLS mode 'require'. native-tls still verifies
    // both certificate chain and hostname, including when the URI says require.
    let mut cfg: tokio_postgres::Config = uri.replace("sslmode=verify-full", "sslmode=require").parse().map_err(|_| "invalid database URL")?;
    if env::var("PGXIO_ALLOW_INSECURE_DB").as_deref() == Ok("1") {
        if env::var("PGXIO_DEV").as_deref() != Ok("1") { return Err("insecure database transport requires development mode".into()); }
        cfg.ssl_mode(tokio_postgres::config::SslMode::Disable);
    } else { cfg.ssl_mode(tokio_postgres::config::SslMode::Require); }
    cfg.connect_timeout(Duration::from_secs(5));
    cfg.application_name("pgxio");
    let mut tls = native_tls::TlsConnector::builder();
    if let Ok(path) = env::var("PGXIO_DB_CA_FILE") {
        tls.add_root_certificate(native_tls::Certificate::from_pem(&std::fs::read(path)?)?);
    }
    let mgr = Manager::from_config(cfg, postgres_native_tls::MakeTlsConnector::new(tls.build()?),
        ManagerConfig { recycling_method: RecyclingMethod::Verified });
    Ok(Pool::builder(mgr).max_size(integer("PGXIO_POOL_SIZE", 4, 256)?)
        .runtime(Runtime::Tokio1).wait_timeout(Some(Duration::from_millis(250)))
        .create_timeout(Some(Duration::from_secs(5))).recycle_timeout(Some(Duration::from_secs(2))).build()?)
}

async fn audit(pool: &Pool, contract: &contract::Contract) -> Result<(), Error> {
    let c = pool.get().await?;
    let row = c.query_one("SELECT current_user = 'web' AND session_user = 'web' AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls) AND NOT has_schema_privilege(current_user, 'web', 'CREATE') FROM pg_roles WHERE rolname = current_user", &[]).await?;
    if !row.get::<_, bool>(0) { return Err("runtime must use the restricted web role without schema CREATE".into()); }
    for e in &contract.endpoints {
        let signature = format!("{}(jsonb)", e.function);
        let row = c.query_one("SELECT has_function_privilege(current_user, $1, 'EXECUTE')", &[&signature]).await?;
        if !row.get::<_, bool>(0) { return Err(format!("missing EXECUTE for {}", e.name).into()); }
    }
    Ok(())
}

async fn call(State(app): State<Arc<App>>, Path(name): Path<String>, request: Request) -> Result<Response, ApiError> {
    let _permit = app.admission.try_acquire().map_err(|_| unavailable())?;
    let endpoint = app.endpoints.get(&name).ok_or(ApiError(StatusCode::NOT_FOUND,"not_found","Unknown endpoint"))?;
    let authorization = request.headers().get(header::AUTHORIZATION).and_then(|v| v.to_str().ok()).and_then(|v| v.strip_prefix("Bearer "))
        .ok_or(ApiError(StatusCode::UNAUTHORIZED,"unauthorized","A valid bearer token is required"))?;
    let claims = app.auth.verify(authorization).await.map_err(|_| ApiError(StatusCode::UNAUTHORIZED,"unauthorized","A valid bearer token is required"))?;
    let media = request.headers().get(header::CONTENT_TYPE).and_then(|v| v.to_str().ok()).unwrap_or("");
    if media.split(';').next().unwrap_or("").trim() != "application/json" {
        return Err(ApiError(StatusCode::UNSUPPORTED_MEDIA_TYPE,"content_type","Expected application/json"));
    }
    let bytes = tokio::time::timeout(Duration::from_secs(5), to_bytes(request.into_body(), app.body_limit)).await
        .map_err(|_| ApiError(StatusCode::REQUEST_TIMEOUT,"request_timeout","Request body deadline exceeded"))?
        .map_err(|_| ApiError(StatusCode::PAYLOAD_TOO_LARGE,"body_limit","Request body exceeds the configured limit"))?;
    let input: Value = serde_json::from_slice(&bytes).map_err(|_| ApiError(StatusCode::BAD_REQUEST,"invalid_json","Malformed JSON"))?;
    if !endpoint.input.is_valid(&input) { return Err(ApiError(StatusCode::BAD_REQUEST,"invalid_input","Request does not match endpoint contract")); }
    let mut connection = app.pool.get().await.map_err(|_| unavailable())?;
    // If the outer deadline interrupts a query/commit, never recycle that connection.
    // Dropping it closes the session and makes an unfinished transaction roll back.
    let result = tokio::time::timeout(app.timeout + Duration::from_secs(1), async {
        let tx = connection.build_transaction().read_only(endpoint.spec.read_only).start().await.map_err(db_error)?;
        let timeout = format!("{}ms", app.timeout.as_millis());
        let claims = claims.to_string();
        tx.query_one("SELECT set_config('request.jwt.claims', $1, true), set_config('statement_timeout', $2, true), set_config('lock_timeout', $2, true), set_config('search_path', 'pg_catalog,web', true)", &[&claims, &timeout]).await.map_err(db_error)?;
        let sql = format!("SELECT {}($1::jsonb)", endpoint.spec.function);
        let row = tx.query_one(&sql, &[&input]).await.map_err(db_error)?;
        let output: Value = row.try_get(0).map_err(|_| ApiError(StatusCode::INTERNAL_SERVER_ERROR,"invalid_output","Endpoint must return JSONB"))?;
        if !endpoint.output.is_valid(&output) { return Err(ApiError(StatusCode::INTERNAL_SERVER_ERROR,"invalid_output","Response does not match endpoint contract")); }
        let body = serde_json::to_vec(&output).map_err(|_| unavailable())?;
        if body.len() > app.response_limit { return Err(ApiError(StatusCode::INTERNAL_SERVER_ERROR,"response_limit","Response exceeds the configured limit")); }
        tx.commit().await.map_err(db_error)?;
        Ok::<_, ApiError>(body)
    }).await;
    let body = match result {
        Ok(Ok(body)) => body,
        Ok(Err(error)) => {
            // Close on any failed operation, including output validation failures.
            drop(deadpool_postgres::Object::take(connection));
            return Err(error);
        }
        Err(_) => {
            drop(deadpool_postgres::Object::take(connection));
            return Err(ApiError(StatusCode::GATEWAY_TIMEOUT,"outcome_unknown","Deadline exceeded; a write may have committed. Do not retry automatically"));
        }
    };
    Ok(([(header::CONTENT_TYPE,"application/json"),(header::CACHE_CONTROL,"no-store")], Body::from(body)).into_response())
}

async fn ready(State(app): State<Arc<App>>) -> Result<Json<Value>, ApiError> {
    tokio::time::timeout(Duration::from_secs(1), async {
        let c = app.pool.get().await.map_err(|_| unavailable())?;
        c.simple_query("SELECT 1").await.map_err(db_error)?;
        Ok(Json(json!({"status":"ready"})))
    }).await.map_err(|_| unavailable())?
}

#[tokio::main(worker_threads = 2)]
async fn main() {
    if let Err(error) = run().await {
        // Configuration messages never include a database URL or JWT.
        eprintln!("{}", json!({"level":"error","message":error.to_string()}));
        std::process::exit(1);
    }
}
async fn run() -> Result<(), Error> {
    let command = env::args().nth(1).unwrap_or_else(|| "serve".into());
    let path = env::var("PGXIO_CONTRACT").unwrap_or_else(|_| "pgxio.json".into());
    let contract = contract::Contract::load(&path)?;
    if command == "openapi" { println!("{}", serde_json::to_string_pretty(&contract.openapi())?); return Ok(()); }
    if command == "validate" { println!("{}",json!({"valid":true,"endpoints":contract.endpoints.len()})); return Ok(()); }
    let pool = pool().await?;
    audit(&pool, &contract).await.map_err(|e| format!("database check failed: {e}"))?;
    if command == "check" { println!("{}",json!({"ready":true,"role":"web","endpoints":contract.endpoints.len()})); return Ok(()); }
    if command != "serve" { return Err("commands: serve, validate, openapi, check".into()); }
    let auth = auth::Auth::from_env().await?;
    let endpoints = contract.endpoints.iter().map(|e| Ok((e.name.clone(), Endpoint { spec:e.clone(),
        input: jsonschema::validator_for(&e.input).map_err(|e| e.to_string())?, output:jsonschema::validator_for(&e.output).map_err(|e| e.to_string())? }))).collect::<Result<HashMap<_,_>,String>>()?;
    let app = Arc::new(App { pool, auth, endpoints, openapi:contract.openapi(),
        admission: Semaphore::new(integer("PGXIO_MAX_INFLIGHT",32,4096)?),
        timeout: Duration::from_millis(integer("PGXIO_QUERY_TIMEOUT_MS",2000,60000)? as u64),
        body_limit: integer("PGXIO_BODY_LIMIT",65536,1048576)?, response_limit: integer("PGXIO_RESPONSE_LIMIT",1048576,16777216)? });
    let origins = env::var("PGXIO_CORS_ORIGINS").unwrap_or_default().split(',').filter(|s| !s.is_empty())
        .map(|s| { if s == "*" { return Err("wildcard CORS is not supported".into()); } s.parse::<HeaderValue>().map_err(|_| "invalid CORS origin".into()) }).collect::<Result<Vec<_>,Error>>()?;
    let router = Router::new().route("/healthz",get(|| async { Json(json!({"status":"ok"})) }))
        .route("/readyz",get(ready)).route("/openapi.json",get(|State(a):State<Arc<App>>| async move { Json(a.openapi.clone()) }))
        .route("/v1/{name}",post(call)).fallback(|| async { ApiError(StatusCode::NOT_FOUND,"not_found","Unknown endpoint") })
        .layer(CorsLayer::new().allow_origin(origins).allow_methods([Method::POST]).allow_headers([header::AUTHORIZATION,header::CONTENT_TYPE]))
        .with_state(app);
    let bind = env::var("PGXIO_BIND").unwrap_or_else(|_| "127.0.0.1:8080".into());
    let listener = tokio::net::TcpListener::bind(&bind).await?;
    eprintln!("{}",json!({"level":"info","event":"listening","address":bind}));
    axum::serve(listener,router).with_graceful_shutdown(async { let _ = tokio::signal::ctrl_c().await; }).await?;
    Ok(())
}
