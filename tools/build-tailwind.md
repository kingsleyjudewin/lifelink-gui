# Rebuilding the inlined Tailwind CSS

`index.html` used to load Tailwind from `cdn.tailwindcss.com` and compile utility
classes in the browser at runtime. That CDN script is unreliable on restricted
networks (ad-blockers, corporate firewalls, some ISPs) and failing to load it
silently strips **all** styling — the page falls back to unstyled HTML.

To make the app work fully offline / on any network, the compiled Tailwind CSS
is now precompiled and inlined directly into the first `<style>` block in
`index.html`'s `<head>`, in place of the old `<script src="https://cdn.tailwindcss.com">`
+ `tailwind.config` script pair. There is no build step at runtime.

**Consequence:** if you add a *new* Tailwind utility class to `index.html` that
wasn't used before (e.g. a new arbitrary value like `mt-[13px]`), it will not
have any effect until you rebuild and re-inline the CSS, since there is no
JIT compiler running in the browser anymore.

## Rebuild steps

1. Get the standalone Tailwind v3 CLI (no npm/node_modules required):
   ```bash
   curl -sL -o tailwindcss \
     https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/tailwindcss-linux-x64
   chmod +x tailwindcss
   ```
   (Swap the asset name for your platform, e.g. `tailwindcss-macos-arm64`.)

2. From this `tools/` directory, build against the real `index.html`:
   ```bash
   ./tailwindcss -i ./input.css -o ./output.min.css \
     --config ./tailwind.config.js --minify
   ```

3. Replace the contents of the first `<style>` block in `index.html`'s
   `<head>` (immediately after the `<title>`, currently marked
   "Tailwind CSS — precompiled & inlined at build time") with the contents
   of `output.min.css`.

`tailwind.config.js` sets the single font family (Geist/Calibri for both the
`sans` and `mono` families, per the single-font-family requirement) and the
border-radius scale (`rounded` = 6px, `rounded-lg` = 12px).

Always rebuild after adding a class instead of hand-patching utilities into a
second `<style>` block — that is how the unclosed `<style>` tag bug happened.
