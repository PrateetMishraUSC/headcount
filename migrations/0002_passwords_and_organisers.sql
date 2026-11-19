-- Optional passwords, and club organisers.
-- Only adds things, so the previous version of the code keeps working.

-- NULL = no password: signing in with just the display name still works.
ALTER TABLE users ADD COLUMN password_hash text;

-- Who may edit a club and its events, and add other organisers.
CREATE TABLE club_organisers (
    club_id    bigint NOT NULL REFERENCES clubs (id),
    user_id    bigint NOT NULL REFERENCES users (id),
    added_by   bigint REFERENCES users (id),   -- NULL: added from the command line
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (club_id, user_id)
);
CREATE INDEX club_organisers_user_idx ON club_organisers (user_id);
