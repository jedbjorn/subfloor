-- Feature #54 / spec #149 — preset and ordinary-conversation route identity.
--
-- Existing preset and conversation rows remain legacy: preset effort stays
-- NULL and conversation contract version defaults to one.  Application code
-- writes version two only with a complete canonical binding.

BEGIN;

ALTER TABLE flavor_defaults ADD COLUMN effort TEXT CHECK (
  effort IS NULL OR (
    trim(effort)<>''
    AND effort=lower(trim(effort))
  )
);

ALTER TABLE conversations ADD COLUMN route_contract_version INTEGER
  NOT NULL DEFAULT 1 CHECK (route_contract_version IN (1,2));
ALTER TABLE conversations ADD COLUMN route_binding TEXT CHECK (
  route_binding IS NULL OR (
    json_valid(route_binding)
    AND json_type(route_binding)='object'
  )
);

CREATE TRIGGER conversations_route_contract_insert
BEFORE INSERT ON conversations
WHEN (NEW.route_contract_version=1 AND NEW.route_binding IS NOT NULL)
  OR (NEW.route_contract_version=2 AND NEW.route_binding IS NULL)
BEGIN
  SELECT RAISE(ABORT, 'conversation route contract and binding disagree');
END;

CREATE TRIGGER conversations_route_contract_update
BEFORE UPDATE OF route_contract_version,route_binding ON conversations
WHEN (NEW.route_contract_version=1 AND NEW.route_binding IS NOT NULL)
  OR (NEW.route_contract_version=2 AND NEW.route_binding IS NULL)
BEGIN
  SELECT RAISE(ABORT, 'conversation route contract and binding disagree');
END;

CREATE TRIGGER conversations_route_identity_immutable
BEFORE UPDATE OF harness,provider,model,effort,
                 route_contract_version,route_binding ON conversations
WHEN NEW.harness IS NOT OLD.harness
  OR NEW.provider IS NOT OLD.provider
  OR NEW.model IS NOT OLD.model
  OR NEW.effort IS NOT OLD.effort
  OR NEW.route_contract_version IS NOT OLD.route_contract_version
  OR NEW.route_binding IS NOT OLD.route_binding
BEGIN
  SELECT RAISE(ABORT, 'conversation route identity is immutable');
END;

COMMIT;
