-- Whether a repo is private (card #602): its plugin step passes a clone token,
-- and a deployment's backfill must be given one to clone it. The collector
-- sets it from each plugin run that cloned the repo, so a deployment knows
-- before it clones; the token itself is never stored. `url` still holds no
-- credentials (its CHECK refuses `@`), private or not.
ALTER TABLE repo ADD COLUMN private boolean NOT NULL DEFAULT false;
