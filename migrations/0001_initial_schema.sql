-- Initial schema: users, clubs, events, RSVPs and notifications.

CREATE TABLE users (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    display_name text NOT NULL
                 CHECK (char_length(display_name) BETWEEN 1 AND 40
                        AND display_name = btrim(display_name)),
    created_at   timestamptz NOT NULL DEFAULT now()
);
-- "Alice" and "alice" are the same person when signing in.
CREATE UNIQUE INDEX users_display_name_key ON users (lower(display_name));

CREATE TABLE clubs (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    description text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE events (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    club_id     bigint NOT NULL REFERENCES clubs (id),
    title       text NOT NULL,
    description text NOT NULL DEFAULT '',
    location    text NOT NULL,
    starts_at   timestamptz NOT NULL,
    capacity    integer NOT NULL CHECK (capacity > 0),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX events_club_id_starts_at_idx ON events (club_id, starts_at);

-- One row per RSVP attempt. Cancelling keeps the row (status = 'cancelled')
-- so reports can count cancellations; RSVPing again creates a new row at the
-- back of the queue.
CREATE TABLE rsvps (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id     bigint NOT NULL REFERENCES events (id),
    user_id      bigint NOT NULL REFERENCES users (id),
    status       text NOT NULL CHECK (status IN ('confirmed', 'waitlisted', 'cancelled')),
    created_at   timestamptz NOT NULL DEFAULT now(),  -- waitlist order: (created_at, id)
    confirmed_at timestamptz,
    cancelled_at timestamptz,
    CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL)),
    CHECK (status <> 'confirmed' OR confirmed_at IS NOT NULL)
);
-- At most one live (confirmed or waitlisted) RSVP per person per event.
CREATE UNIQUE INDEX rsvps_one_active_per_user
    ON rsvps (event_id, user_id) WHERE status <> 'cancelled';
-- The waitlist in order, and the confirmed head count, per event.
CREATE INDEX rsvps_waitlist_idx  ON rsvps (event_id, created_at, id) WHERE status = 'waitlisted';
CREATE INDEX rsvps_confirmed_idx ON rsvps (event_id) WHERE status = 'confirmed';
-- "Your RSVPs".
CREATE INDEX rsvps_user_idx ON rsvps (user_id, created_at DESC);

CREATE TABLE notifications (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    bigint NOT NULL REFERENCES users (id),
    event_id   bigint NOT NULL REFERENCES events (id),
    -- Set for "you got a spot" notifications; UNIQUE means one promotion can
    -- never produce two notifications, even if two workers misbehave.
    rsvp_id    bigint UNIQUE REFERENCES rsvps (id),
    message    text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    read_at    timestamptz
);
CREATE INDEX notifications_user_idx ON notifications (user_id, created_at DESC);
