BEGIN;

CREATE OR REPLACE FUNCTION gateway_lookup_api_key(p_key_id text)
RETURNS TABLE(merchant_id text, secret_ciphertext bytea, scopes text[], mode text)
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT k.merchant_id, k.secret_ciphertext, k.scopes, k.mode
      FROM merchant_api_keys k JOIN merchants m USING (merchant_id)
     WHERE k.key_id = p_key_id AND k.revoked_at IS NULL
       AND (k.expires_at IS NULL OR k.expires_at > clock_timestamp())
       AND m.status = 'active'
$$;

REVOKE SELECT ON merchants, merchant_api_keys FROM tally_gateway_app;
GRANT EXECUTE ON FUNCTION gateway_lookup_api_key(text) TO tally_gateway_app;

INSERT INTO gateway_schema_migrations(version) VALUES (4) ON CONFLICT DO NOTHING;
COMMIT;
