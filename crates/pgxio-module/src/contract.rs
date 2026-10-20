use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{collections::HashSet, error::Error};

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Contract {
    pub version: u32,
    pub endpoints: Vec<Endpoint>,
    #[serde(default)]
    pub streams: Vec<Stream>,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Stream {
    pub name: String,
    #[serde(skip)]
    pub authorize: String,
    pub input: Value,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Endpoint {
    pub name: String,
    #[serde(skip)]
    pub function: String,
    #[serde(skip)]
    pub read_only: bool,
    pub input: Value,
    pub output: Value,
}

pub fn identifier(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 63
        && s.bytes()
            .enumerate()
            .all(|(i, c)| c.is_ascii_lowercase() || c == b'_' || (i > 0 && c.is_ascii_digit()))
}

// Contracts are local, self-contained schemas. Never fetch refs supplied in a manifest.
fn no_references(value: &Value) -> bool {
    match value {
        Value::Object(o) => {
            !o.contains_key("$ref")
                && !o.contains_key("$dynamicRef")
                && o.values().all(no_references)
        }
        Value::Array(a) => a.iter().all(no_references),
        _ => true,
    }
}

impl Contract {
    pub fn load(path: &str) -> Result<Self, Box<dyn Error + Send + Sync>> {
        let bytes = std::fs::read(path)?;
        if bytes.len() > 1_048_576 {
            return Err("contract exceeds 1 MiB".into());
        }
        let c: Self = serde_json::from_slice(&bytes)?;
        c.validate()?;
        Ok(c)
    }

    pub fn validate(&self) -> Result<(), Box<dyn Error + Send + Sync>> {
        if self.version != 2 {
            return Err("expected contract version 2".into());
        }
        let mut names = HashSet::new();
        for e in &self.endpoints {
            if !identifier(&e.name) || !names.insert(&e.name) {
                return Err("invalid or duplicate endpoint name".into());
            }
            for s in [&e.input, &e.output] {
                if !s.is_object() || !no_references(s) {
                    return Err("schemas must be objects without references".into());
                }
                jsonschema::validator_for(s).map_err(|e| format!("invalid schema: {e}"))?;
            }
        }
        names.clear();
        for s in &self.streams {
            if !identifier(&s.name) || !names.insert(&s.name) {
                return Err("invalid stream name or authorization function".into());
            }
            if !s.input.is_object() || !no_references(&s.input) {
                return Err("invalid stream schema".into());
            }
            jsonschema::validator_for(&s.input).map_err(|e| e.to_string())?;
        }
        Ok(())
    }

    pub fn openapi(&self) -> Value {
        let mut paths = serde_json::Map::new();
        for e in &self.endpoints {
            paths.insert(format!("/v1/{}", e.name), json!({"post": {
                "operationId": e.name, "security": [{"bearerAuth": []}],
                "requestBody": {"required":true,"content":{"application/json":{"schema":e.input}}},
                "responses": {
                    "200":{"description":"Success","content":{"application/json":{"schema":e.output}}},
                    "default":{"description":"Error","content":{"application/json":{"schema":{"type":"object","required":["error"],"properties":{"error":{"type":"object","required":["code"],"properties":{"code":{"type":"string"}}}}}}}}
                }
            }}));
        }
        json!({"openapi":"3.1.0","info":{"title":"pgxio","version":"0.1.0"},"paths":paths,
            "components":{"securitySchemes":{"bearerAuth":{"type":"http","scheme":"bearer","bearerFormat":"JWT"}}}})
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_sql_identifiers_and_remote_refs() {
        let mut c: Contract = serde_json::from_value(json!({"version":2,"endpoints":[{
            "name":"hello","input":{"type":"object"},"output":{"type":"object"}
        }]}))
        .unwrap();
        assert!(c.validate().is_ok());
        c.endpoints[0].name = "hello; drop schema web".into();
        assert!(c.validate().is_err());
        c.endpoints[0].name = "hello".into();
        c.endpoints[0].input = json!({"$ref":"https://example.com/schema"});
        assert!(c.validate().is_err());
    }
}
