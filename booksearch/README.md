# Book Search

Book Search adds an internet-catalog fallback to Nicotine+ chat. It targets
Nicotine+ 3.3.11 and Python 3.9 or newer, performs network requests outside the
UI thread, and sends all Nicotine+ messages from the main thread.

The initial provider is [Open Library](https://openlibrary.org/developers/api).
Results link to catalog/work pages. Availability labels such as `public` or
`borrowable` come from Open Library; the plugin does not download books or
bypass a provider's access controls.

## Commands

- `/booksearch <title, author, or ISBN>` searches from a room or private-chat
  tab. `/books` is an alias.
- `!book <title, author, or ISBN>` lets another user request a search after
  **Allow remote requests** is enabled in the plugin settings.

Remote requests are disabled by default. They can be limited to a
comma-separated room allowlist and are rate-limited per user. Private-message
requests follow the same enable switch and cooldown.

## Install for local review

Copy the complete `booksearch` directory into the Nicotine+ user plugins
directory, restart Nicotine+, then enable **Book Search** under
Preferences → Plugins. No third-party Python packages are required.

## Adding another provider

Keep provider-specific HTTP and response parsing in a class with:

```python
name = "Catalog name"

def search(self, query, limit, timeout):
    return [SearchResult(...)]
```

Add an instance to `Plugin.providers`. Providers should use documented APIs,
HTTPS, bounded timeouts, and links that respect the site's access rules. Do not
put credentials in source code or log them. Site-specific HTML scraping should
be kept separate because markup and anti-bot behavior are unstable.

Karakeep can help identify the next provider: export or copy the remembered
bookmark URL, then add a provider only after its API/access rules are known.
