use jsonwebtoken::{decode, decode_header, jwk::JwkSet, Algorithm, DecodingKey, Validation};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    cell::RefCell,
    collections::{BTreeSet, HashMap},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

#[derive(Clone, Deserialize, Serialize)]
pub struct Claims {
    pub sub: String,
    pub exp: u64,
    pub iss: String,
    pub aud: Value,
}
type Digest = [u8; 32];
struct Entry {
    claims: Claims,
    expires: Instant,
    used: Instant,
    bytes: usize,
}
#[derive(Default)]
struct Cache {
    entries: HashMap<Digest, Entry>,
    lru: BTreeSet<(Instant, Digest)>,
    expiry: BTreeSet<(Instant, Digest)>,
    bytes: usize,
    max_entries: usize,
    max_bytes: usize,
}
impl Cache {
    fn remove(&mut self, key: &Digest) {
        if let Some(entry) = self.entries.remove(key) {
            self.lru.remove(&(entry.used, *key));
            self.expiry.remove(&(entry.expires, *key));
            self.bytes -= entry.bytes;
        }
    }
    fn prune(&mut self, now: Instant) {
        while let Some(&(expiry, key)) = self.expiry.first() {
            if expiry > now {
                break;
            }
            self.remove(&key);
        }
    }
    fn get(&mut self, key: &Digest, wall: u64, now: Instant) -> Option<Claims> {
        self.prune(now);
        let entry = self.entries.get(key)?;
        if entry.claims.exp <= wall {
            self.remove(key);
            return None;
        }
        let entry = self.entries.get_mut(key).unwrap();
        self.lru.remove(&(entry.used, *key));
        entry.used = now;
        self.lru.insert((now, *key));
        Some(entry.claims.clone())
    }
    fn insert(&mut self, key: Digest, claims: Claims, wall: u64, now: Instant) {
        self.prune(now);
        // Budget serialized claims plus digest; container/allocator overhead is
        // bounded separately by max_entries, not represented as an RSS limit.
        let bytes = serde_json::to_vec(&claims).unwrap().len() + key.len();
        if self.max_entries == 0 || bytes > self.max_bytes || claims.exp <= wall {
            return;
        }
        self.remove(&key);
        while self.entries.len() >= self.max_entries || self.bytes + bytes > self.max_bytes {
            let Some(&(_, oldest)) = self.lru.first() else {
                return;
            };
            self.remove(&oldest);
        }
        let expires = now + Duration::from_secs(30.min(claims.exp - wall));
        self.entries.insert(
            key,
            Entry {
                claims,
                expires,
                used: now,
                bytes,
            },
        );
        self.lru.insert((now, key));
        self.expiry.insert((expires, key));
        self.bytes += bytes;
    }
}
pub struct Auth {
    keys: HashMap<String, DecodingKey>,
    secret: Option<DecodingKey>,
    validations: [Validation; 3],
    cache: RefCell<Cache>,
}
impl Auth {
    pub fn new(
        issuer: String,
        audience: String,
        jwks: Option<&str>,
        dev: bool,
    ) -> Result<Self, String> {
        let mut keys = HashMap::new();
        if let Some(path) = jwks {
            let set: JwkSet =
                serde_json::from_slice(&std::fs::read(path).map_err(|e| e.to_string())?)
                    .map_err(|e| e.to_string())?;
            for jwk in set.keys {
                let kid = jwk
                    .common
                    .key_id
                    .clone()
                    .ok_or("JWKS signing key requires kid")?;
                let key = DecodingKey::from_jwk(&jwk).map_err(|e| e.to_string())?;
                if keys.insert(kid, key).is_some() {
                    return Err("duplicate JWKS kid".into());
                }
            }
        }
        let secret = if dev {
            let secret =
                std::env::var("PGXIO_DEV_SECRET").map_err(|_| "missing PGXIO_DEV_SECRET")?;
            if secret.len() < 32 {
                return Err("dev secret must be at least 32 bytes".into());
            }
            Some(DecodingKey::from_secret(secret.as_bytes()))
        } else {
            None
        };
        if keys.is_empty() && secret.is_none() {
            return Err("production requires a JWKS file".into());
        }
        let policy = |algorithm| {
            let mut v = Validation::new(algorithm);
            v.set_issuer(&[&issuer]);
            v.set_audience(&[&audience]);
            v.set_required_spec_claims(&["sub", "exp", "iss", "aud"]);
            v.leeway = 0;
            v
        };
        Ok(Self {
            keys,
            secret,
            validations: [
                policy(Algorithm::RS256),
                policy(Algorithm::ES256),
                policy(Algorithm::HS256),
            ],
            cache: RefCell::new(Cache::default()),
        })
    }
    pub fn cache_limits(&mut self, entries: usize, bytes: usize) {
        self.cache = RefCell::new(Cache {
            max_entries: entries,
            max_bytes: bytes,
            ..Cache::default()
        });
    }
    pub fn prune(&self) {
        self.cache.borrow_mut().prune(Instant::now());
    }
    pub fn verify(&self, token: &str) -> Result<Claims, ()> {
        if token.len() > 8192 {
            return Err(());
        }
        let now = Instant::now();
        let wall = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| ())?
            .as_secs();
        let digest: Option<Digest> = if self.cache.borrow().max_entries > 0 {
            Some(
                ring::digest::digest(&ring::digest::SHA256, token.as_bytes())
                    .as_ref()
                    .try_into()
                    .unwrap(),
            )
        } else {
            None
        };
        if let Some(key) = &digest {
            if let Some(claims) = self.cache.borrow_mut().get(key, wall, now) {
                return Ok(claims);
            }
        }
        let header = decode_header(token).map_err(|_| ())?;
        let (key, validation) = match header.alg {
            Algorithm::RS256 | Algorithm::ES256 => {
                let kid = header.kid.as_ref().ok_or(())?;
                (
                    self.keys.get(kid).ok_or(())?,
                    &self.validations[if header.alg == Algorithm::RS256 { 0 } else { 1 }],
                )
            }
            Algorithm::HS256 => (self.secret.as_ref().ok_or(())?, &self.validations[2]),
            _ => return Err(()),
        };
        let claims = decode::<Claims>(token, key, validation)
            .map_err(|_| ())?
            .claims;
        if claims.exp <= wall
            || claims.sub.is_empty()
            || claims.sub.len() > 256
            || claims.sub.contains('\0')
        {
            return Err(());
        }
        if let Some(key) = digest {
            self.cache
                .borrow_mut()
                .insert(key, claims.clone(), wall, now);
        }
        Ok(claims)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn claims(exp: u64) -> Claims {
        Claims {
            sub: "alice".into(),
            exp,
            iss: "test".into(),
            aud: Value::String("test".into()),
        }
    }
    #[test]
    fn cache_is_lru_and_bounded_including_expiry_metadata() {
        let mut cache = Cache {
            max_entries: 2,
            max_bytes: 1000,
            ..Cache::default()
        };
        let now = Instant::now();
        cache.insert([1; 32], claims(100), 1, now);
        cache.insert([2; 32], claims(100), 1, now + Duration::from_millis(1));
        assert!(cache
            .get(&[1; 32], 1, now + Duration::from_millis(2))
            .is_some());
        cache.insert([3; 32], claims(100), 1, now + Duration::from_millis(3));
        assert!(!cache.entries.contains_key(&[2; 32]));
        for n in 4..200 {
            cache.insert(
                [n; 32],
                claims(100),
                1,
                now + Duration::from_millis(n.into()),
            );
        }
        assert_eq!(cache.entries.len(), 2);
        assert_eq!(cache.lru.len(), 2);
        assert_eq!(cache.expiry.len(), 2);
        cache.prune(now + Duration::from_secs(31));
        assert!(cache.entries.is_empty());
        assert_eq!(cache.bytes, 0);
    }
    #[test]
    fn cache_enforces_expiry_on_hits_and_payload_budget() {
        let mut cache = Cache {
            max_entries: 2,
            max_bytes: 1000,
            ..Cache::default()
        };
        let now = Instant::now();
        cache.insert([1; 32], claims(3), 1, now);
        assert!(cache.get(&[1; 32], 3, now).is_none());
        cache.max_bytes = 1;
        cache.insert([2; 32], claims(100), 1, now);
        assert!(cache.entries.is_empty());
        cache.max_bytes = 1000;
        cache.insert([2; 32], claims(100), 1, now);
        assert!(cache
            .get(&[2; 32], 1, now + Duration::from_secs(31))
            .is_none());
    }
}
