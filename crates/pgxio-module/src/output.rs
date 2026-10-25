//! The result owns its bytes across PQclear and COMMIT. Nginx copies them on reply.
//! Caller must verify a single non-NULL, text-format JSONB field first.
pub fn capture(bytes: &[u8], schema: Option<&jsonschema::Validator>) -> Result<Vec<u8>, ()> {
    if bytes.len() > 1_048_576 {
        return Err(());
    }
    if let Some(schema) = schema {
        let value: serde_json::Value = serde_json::from_slice(bytes).map_err(|_| ())?;
        if !schema.is_valid(&value) {
            return Err(());
        }
    }
    Ok(bytes.to_vec())
}
