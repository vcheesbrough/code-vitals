-- Where each repo is cloned from (card #564). A deployment re-measures every
-- repo in this table from its URL, reading the repo's own plugin settings, so
-- the table is the list of measured repos: a repo registers itself the first
-- time its plugin step runs. Public repos only, so a URL never holds
-- credentials (nor whitespace, as it is a word in scripts/run-collector.sh).
ALTER TABLE repo
    ADD COLUMN url text CHECK (url !~ '[@[:space:]]' AND url <> '');

-- The repos measured before self-registration, whose rows predate the column.
UPDATE repo SET url = 'https://github.com/vcheesbrough/' || name || '.git'
WHERE url IS NULL AND name IN ('v-note', 'bored', 'sovereign-config');
