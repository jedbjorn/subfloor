-- Sprint 25 seq 5 review (flag #42): force termination is gated on a PRIOR
-- graceful timeout for the session (spec #20 Workflow 9: force "only after
-- graceful termination fails and shows the PID/generation it will end").
-- The timeout is recorded durably so the gate survives a service restart
-- between the graceful attempt and the force follow-up. NULL = no graceful
-- timeout yet → force refused (409 force_requires_graceful_timeout).

ALTER TABLE interface_sessions ADD COLUMN graceful_timed_out_at TEXT;
