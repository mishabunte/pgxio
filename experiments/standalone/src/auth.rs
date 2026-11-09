use jsonwebtoken::{decode, decode_header, jwk::JwkSet, Algorithm, DecodingKey, Validation};
use serde_json::{json, Value};
use std::{env, time::{Duration, Instant}};
use tokio::sync::Mutex;

pub struct Auth {
    issuer: String,
    audience: String,
    dev_secret: Option<Vec<u8>>,
    url: Option<String>,
    keys: Mutex<Keys>,
    http: reqwest::Client,
}
struct Keys { jwks: JwkSet, fetched: Instant }

impl Auth {
    pub async fn from_env() -> Result<Self, Box<dyn std::error::Error + Send + Sync>> {
        let dev = env::var("PGXIO_DEV").as_deref() == Ok("1");
        let secret = env::var("PGXIO_DEV_JWT_SECRET").ok();
        if secret.is_some() && !dev { return Err("development JWT secret requires PGXIO_DEV=1".into()); }
        if secret.as_ref().is_some_and(|s| s.len() < 32) { return Err("development JWT secret must be at least 32 bytes".into()); }
        let issuer = env::var("PGXIO_JWT_ISSUER")?;
        let audience = env::var("PGXIO_JWT_AUDIENCE")?;
        if issuer.is_empty() || audience.is_empty() { return Err("issuer and audience must be nonempty".into()); }
        let url = env::var("PGXIO_JWKS_URL").ok();
        if url.as_ref().is_some_and(|u| !u.starts_with("https://")) { return Err("JWKS URL must use HTTPS".into()); }
        let http = reqwest::Client::builder().timeout(Duration::from_secs(5)).redirect(reqwest::redirect::Policy::none()).build()?;
        let jwks = if let Ok(path) = env::var("PGXIO_JWKS_FILE") {
            serde_json::from_slice(&std::fs::read(path)?)?
        } else if let Some(url) = &url {
            Self::fetch(&http, url).await?
        } else if secret.is_some() { JwkSet { keys: vec![] } }
        else { return Err("configure PGXIO_JWKS_URL or PGXIO_JWKS_FILE".into()); };
        Ok(Self { issuer, audience, dev_secret: secret.map(String::into_bytes), url,
            keys: Mutex::new(Keys { jwks, fetched: Instant::now() }), http })
    }

    async fn fetch(http: &reqwest::Client, url: &str) -> Result<JwkSet, Box<dyn std::error::Error + Send + Sync>> {
        let mut response = http.get(url).send().await?.error_for_status()?;
        let mut body = Vec::new();
        while let Some(chunk) = response.chunk().await? {
            if body.len() + chunk.len() > 65_536 { return Err("JWKS exceeds 64 KiB".into()); }
            body.extend(chunk);
        }
        Ok(serde_json::from_slice(&body)?)
    }

    pub async fn verify(&self, token: &str) -> Result<Value, ()> {
        if token.len() > 8192 { return Err(()); }
        let header = decode_header(token).map_err(|_| ())?;
        let key = if header.alg == Algorithm::HS256 {
            DecodingKey::from_secret(self.dev_secret.as_ref().ok_or(())?)
        } else {
            if !matches!(header.alg, Algorithm::RS256 | Algorithm::ES256) { return Err(()); }
            let kid = header.kid.as_deref().ok_or(())?;
            let mut cache = self.keys.lock().await;
            let age = cache.fetched.elapsed();
            if let Some(url) = &self.url {
                if age > Duration::from_secs(300) || (cache.jwks.find(kid).is_none() && age > Duration::from_secs(30)) {
                    // Timestamp failed refreshes too: unknown kids cannot trigger a fetch per request.
                    cache.fetched = Instant::now();
                    cache.jwks = Self::fetch(&self.http, url).await.map_err(|_| ())?;
                }
            }
            DecodingKey::from_jwk(cache.jwks.find(kid).ok_or(())?).map_err(|_| ())?
        };
        let mut validation = Validation::new(header.alg);
        validation.set_issuer(&[&self.issuer]);
        validation.set_audience(&[&self.audience]);
        validation.set_required_spec_claims(&["exp", "sub", "iss", "aud"]);
        validation.validate_nbf = true;
        validation.leeway = 5;
        let claims = decode::<Value>(token, &key, &validation).map_err(|_| ())?.claims;
        let sub = claims.get("sub").and_then(Value::as_str).filter(|s| !s.is_empty() && s.len() <= 256).ok_or(())?;
        // A valid JWT is not permission to SET ROLE. Only verified identity enters SQL.
        Ok(json!({"sub":sub,"role":"web"}))
    }
}
