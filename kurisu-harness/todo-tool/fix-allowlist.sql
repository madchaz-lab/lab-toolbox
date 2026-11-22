CREATE OR REPLACE FUNCTION kurisu_todo.validate_metadata_agent() RETURNS TRIGGER AS $$
DECLARE
    agent TEXT;
    allowed TEXT[] := ARRAY[
        'orchestrator',
        'kurisu-researcher',
        'kurisu-programming',
        'kurisu-labadmin',
        'kurisu-conversation',
        'kurisu-quality',
        'kurisu-documentation',
        'kurisu-security'
    ];
BEGIN
    IF NEW.metadata IS NOT NULL THEN
        agent := NEW.metadata->>'agent';
        IF agent IS NOT NULL AND NOT (agent = ANY (allowed)) THEN
            RAISE EXCEPTION 'metadata.agent % is not allowed. Allowed: %', agent, allowed;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
