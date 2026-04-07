---
name: mealie
description: Manage Mealie recipe manager - create, search, view, update, and delete recipes; plan meals for the week; list existing tags, categories, foods, and units. Use when the user wants to save a recipe to their Mealie library (you extract the recipe data from URLs/HTML/text yourself, hand the freeform ingredient strings to the skill which parses them via Mealie's own ingredient parser), look up an existing recipe, schedule meals, or view what organizer entries already exist in the database.
metadata:
  openclaw:
    requires:
      bins:
        - python3
      env:
        - MEALIE_URL
        - MEALIE_API_TOKEN
    primaryEnv: MEALIE_API_TOKEN
    install:
      - kind: pip
        package: httpx
      - kind: pip
        package: python-dotenv
      - kind: pip
        package: agent-skill-handler
        source: git+https://github.com/wnagele/agent-skill-handler.git
  openfang:
    requires:
      bins:
        - python3
    install:
      - kind: pip
        package: httpx
      - kind: pip
        package: python-dotenv
      - kind: pip
        package: agent-skill-handler
        source: git+https://github.com/wnagele/agent-skill-handler.git
    env:
      - name: MEALIE_URL
        description: Mealie base URL (e.g. https://mealie.example.com)
      - name: MEALIE_API_TOKEN
        description: Mealie API token
        secret: true
---

# Mealie Recipe Manager Integration

Manage Mealie — create and view recipes, plan meals, and list existing organizers (tags, categories, foods, units).

This skill is a thin wrapper around Mealie's REST API. **Recipe extraction from URLs is NOT done by this skill** — you (the agent) fetch the URL with WebFetch, pick out the recipe fields, and pipe structured JSON to `recipe create`. Ingredients, however, can be passed as **freeform strings** — the skill sends them through Mealie's built-in ingredient parser (`POST /api/parser/ingredients`), which resolves unit and food references against the user's existing database automatically. You do not need to normalize ingredient names yourself.

For tags, categories, tools, units, and foods that don't exist yet, the skill does a **lookup-or-create** pass before writing the recipe: it fetches the current lists, reuses existing entries by exact name, and POSTs anything new to the appropriate `/api/organizers/...`, `/api/units`, or `/api/foods` endpoint.

## Setup

Provide `MEALIE_URL` and `MEALIE_API_TOKEN` via environment variables or a `.env` file:

```
MEALIE_URL=https://mealie.example.com
MEALIE_API_TOKEN=ey...
```

The skill checks environment variables first, then falls back to a `.env` file (searched from the current directory upward).

To generate an API token:
1. Sign in to Mealie
2. Navigate to **User Profile → Manage Your API Tokens**
3. Enter a token name (e.g. `mealie-cli`) and click **Generate**
4. Copy the token immediately — it is only shown once

Mealie v2.0+ is required for meal-plan endpoints.

## Workflow: saving a recipe from a URL

Because the skill doesn't scrape URLs, the canonical "save this recipe" flow is:

1. **Fetch the source.** Use WebFetch (or another tool you have) to get the page HTML. If the page has schema.org `Recipe` JSON-LD, parse it directly. Otherwise extract fields from the HTML.
2. **Construct the recipe JSON** in the shape documented below. Pass `recipeIngredient` as a list of **freeform strings** (e.g. `"2 cups all-purpose flour, sifted"`). The skill sends them through Mealie's ingredient parser during `recipe create` and resolves unit / food references against the existing database.
3. **Pipe it to `recipe create`:**
   ```bash
   echo '<json>' | python3 scripts/mealie.py recipe create -
   ```
   Or write to a temp file and pass the path. Both work.

The CLI handles Mealie's two-step create dance internally (`POST /api/recipes` to obtain a slug, then `PUT /api/recipes/{slug}` with the full body), the ingredient-string batch parse, the lookup-or-create pass for tags / categories / any newly-named units or foods, and finally re-fetches the canonical state to print a summary.

**If you want to inspect what the parser does** before committing to a full recipe create, use `recipe parse-ingredients`:

```bash
python3 scripts/mealie.py recipe parse-ingredients \
  "2 cups all-purpose flour, sifted" \
  "1 tbsp olive oil"
```

It emits the parser's structured output as JSON so you can verify that unit / food matching behaved the way you expected.

## Recipe JSON shape

The minimum: `{"name": "..."}`. Everything else is optional. A complete example:

```json
{
  "name": "Chocolate Chip Cookies",
  "description": "Classic chewy chocolate chip cookies with a crisp edge.",
  "recipeYield": "24 cookies",
  "prepTime": "PT15M",
  "cookTime": "PT12M",
  "totalTime": "PT45M",
  "orgURL": "https://www.kingarthurbaking.com/recipes/chocolate-chip-cookies-recipe",
  "tags":           ["dessert", "baking"],
  "recipeCategory": ["Desserts"],
  "recipeIngredient": [
    "1 cup unsalted butter, softened",
    "3/4 cup granulated sugar",
    "3/4 cup packed brown sugar",
    "2 large eggs",
    "1 tsp vanilla extract",
    "2 1/4 cups all-purpose flour",
    "1 tsp baking soda",
    "1 tsp salt",
    "2 cups semisweet chocolate chips",
    {"disableAmount": true, "note": "Flaky sea salt for finishing"}
  ],
  "recipeInstructions": [
    {"text": "Preheat oven to 375°F (190°C). Line two baking sheets with parchment."},
    {"text": "In a large bowl, cream butter and sugars until light and fluffy."},
    {"text": "Beat in eggs one at a time, then stir in vanilla."}
  ],
  "tools": ["Baking Sheet"],
  "notes": [
    {"title": "Tip", "text": "Refrigerate the dough for 1 hour for chewier cookies."}
  ],
  "nutrition": {
    "calories": "180",
    "fatContent": "9",
    "carbohydrateContent": "24",
    "proteinContent": "2"
  }
}
```

Rules:

- **`name`** is the only required field.
- **Times** use ISO-8601 durations (`PT15M`, `PT1H30M`). Omit fields you don't know.
- **Ingredients** should normally be freeform strings. The skill batch-parses them through Mealie's ingredient parser, which already knows how to extract quantities, units, foods, and notes, and will match unit/food names against the user's existing database. You only need to use a structured dict for entries the parser will struggle with — typically free-text items like "flaky sea salt to taste" — and for those use `{"disableAmount": true, "note": "..."}`. If you do provide a structured dict with `unit: {"name": "..."}` or `food: {"name": "..."}`, the skill will look up or create the referenced entries before submitting.
- **Tags and categories** can be either plain strings (`"dessert"`) or objects (`{"name": "dessert"}`). The skill will look up existing entries by name and POST new ones if missing. You do not need to provide slugs or IDs.
- **Tools** (`tools` field) can be either plain strings (`"stand mixer"`) or objects (`{"name": "stand mixer"}`). The skill looks up existing tools by name and POSTs new ones if missing, same as tags/categories.
- **`orgURL`** is the source URL; `recipe show` displays it as `Source:`.
- **`nutrition`** fields are all strings. Omit the whole object if unknown.

If you want to see what the parser does with specific ingredient strings before committing to a full `recipe create`, call `recipe parse-ingredients` directly — it prints the parser's structured output as JSON.

## Commands

### `recipe create`

```bash
python3 scripts/mealie.py recipe create <json-file-or-->
```

Create a new recipe from a JSON file or stdin (`-`). Validates that the JSON has a non-empty `name`, normalizes tags/categories, posts the stub, then PUTs the full body. Prints a summary including the new slug. Warns to stderr if the resulting recipe has no ingredients or no instructions.

### `recipe update`

```bash
python3 scripts/mealie.py recipe update <recipe> <json-file-or-->
```

Patch an existing recipe with partial JSON. `<recipe>` is resolved by slug → UUID → title substring (case-insensitive). The typical read-modify-write loop is:

```bash
python3 scripts/mealie.py recipe show <slug> --json \
  | jq '.description = "Updated Description"' \
  | python3 scripts/mealie.py recipe update <slug> -
```

### `recipe search`

```bash
python3 scripts/mealie.py recipe search [<query>] [--tag T ...] [--category C ...] [--cookbook K] [--limit N] [--asc|--desc]
```

Search recipes by name substring, tag(s), category(ies), or cookbook. Multiple `--tag` / `--category` flags require ALL to match (AND semantics). Default sort is name ascending; pass `--desc` to reverse. Default limit is 20.

### `recipe show`

```bash
python3 scripts/mealie.py recipe show <recipe> [--json]
```

Display one recipe. Without `--json`, prints a formatted block with metadata, description, ingredients, instructions, notes, and a back-link to the Mealie web UI. With `--json`, emits the raw recipe JSON suitable for piping to `recipe update -`.

### `recipe random`

```bash
python3 scripts/mealie.py recipe random [--tag T ...] [--category C ...]
```

Pick one random recipe (optionally filtered by tag/category) and print it in the same format as `recipe show`. Useful for "what should I cook tonight?" prompts.

### `recipe delete`

```bash
python3 scripts/mealie.py recipe delete <recipe> [--yes]
```

Permanently delete a recipe. The `--yes` flag is required — without it the command refuses to proceed and prints what it would have deleted. There is no undo.

### `recipe parse-ingredients`

```bash
python3 scripts/mealie.py recipe parse-ingredients "<string>" ["<string>" ...] [--stdin] [--parser nlp|brute|openai]
```

Batch-parse freeform ingredient strings via `POST /api/parser/ingredients` and print the structured result as JSON. Useful for:
- Debugging recipe imports where `recipe create` produces unexpected quantity/unit/food fields
- Previewing what the parser will do before committing to a full recipe create
- Letting the agent inspect parser output and make decisions based on it (e.g. flag low-confidence matches)

Pass strings as positional arguments, or use `--stdin` to read one per line from stdin. The `--parser` flag overrides Mealie's default; the `openai` parser requires Mealie's OpenAI integration to be configured on the server.

### `mealplan today`

```bash
python3 scripts/mealie.py mealplan today
```

List today's planned meals, grouped by entry type. Each line includes the entry's numeric `[id N]` for use with `mealplan delete`.

### `mealplan week`

```bash
python3 scripts/mealie.py mealplan week [--start <date>]
```

Show a 7-day window of meals starting from `<date>` (defaults to today). `<date>` accepts ISO `YYYY-MM-DD`, `today`, `tomorrow`, `yesterday`, or weekday names (which resolve to the next occurrence including today).

### `mealplan add`

```bash
python3 scripts/mealie.py mealplan add <date> <type> [<recipe>] [--title T] [--text T]
```

Add a meal plan entry. `<type>` is one of: `breakfast`, `lunch`, `dinner`, `side`, `snack`, `drink`, `dessert`. Pass either `<recipe>` (a slug, UUID, or title substring) **or** `--title` / `--text` for a free-text entry — not both.

### `mealplan random`

```bash
python3 scripts/mealie.py mealplan random <date> <type>
```

Add a random recipe to the meal plan, respecting the household's mealplan rules (configured separately in the Mealie web UI).

### `mealplan delete`

```bash
python3 scripts/mealie.py mealplan delete <id>
```

Delete a meal plan entry by its numeric `id` (from the `[id N]` annotation in `mealplan today` / `mealplan week` output).

### `organizers list`

```bash
python3 scripts/mealie.py organizers list <kind> [--limit N]
```

List all entries for an organizer kind. `<kind>` is one of: `tags`, `categories`, `tools`, `foods`, `units`, `labels`, `cookbooks`. The output is a plain bullet list of names — for `units`, the abbreviation is shown in parentheses when present. Use this **before** `recipe create` to normalize ingredient names against what already exists.

## Examples

| User says | Command |
|---|---|
| "Save this recipe: https://..." | (1) fetch URL with WebFetch + extract fields, (2) build JSON with `recipeIngredient` as an array of freeform strings, (3) `echo '<json>' \| recipe create -` |
| "Add this recipe from the page I'm looking at" | Same: build JSON with ingredient strings, pipe to `recipe create -` |
| "How would Mealie parse 'a generous pinch of cracked black pepper'?" | `recipe parse-ingredients "a generous pinch of cracked black pepper"` |
| "Edit my chocolate chip cookies — change the description" | `recipe show "chocolate chip cookies" --json \| jq '.description = "..."' \| recipe update "chocolate chip cookies" -` |
| "Find my chocolate chip cookies" | `recipe show "chocolate chip cookies"` |
| "What dessert recipes do I have?" | `recipe search "" --category "Desserts"` |
| "Search for pasta recipes that are vegetarian" | `recipe search pasta --tag vegetarian` |
| "Pick a random vegetarian dinner" | `recipe random --tag vegetarian` |
| "Delete that recipe" | `recipe delete <slug> --yes` |
| "Export the cookies recipe as JSON" | `recipe show "chocolate chip cookies" --json` |
| "What ingredients have I used before?" | `organizers list foods` |
| "What units does Mealie know?" | `organizers list units` |
| "What tags can I filter by?" | `organizers list tags` |
| "What categories exist?" | `organizers list categories` |
| "What's for dinner tonight?" | `mealplan today` |
| "What's on the menu this week?" | `mealplan week` |
| "Plan tacos for Tuesday dinner" | `mealplan add tuesday dinner "tacos"` |
| "Add garlic bread as a side today" | `mealplan add today side --title "Garlic bread"` |
| "Pick a random dinner for tomorrow" | `mealplan random tomorrow dinner` |
| "Cancel the dinner I planned for Tuesday" | `mealplan delete <id>` (id from `mealplan week` output) |

## Notes

- **Mealie version**: v2.0+ required for the `mealplan` commands (households were split from groups in v2.0).
- **Demo instance**: `https://demo.mealie.io` resets daily. It is fine for testing this skill but any data you create there will be wiped.
- **Ingredient parsing**: the skill uses Mealie's ingredient parser endpoint, which runs on the Mealie server. Quality depends on which parser the server has enabled (`nlp`, `brute`, or `openai`) and on the user's existing food/unit database — the parser matches against those. You can pass `--parser` to `recipe parse-ingredients` to force a specific one; `recipe create` uses whatever the server default is.
- **Organizer dedup**: tags / categories / units / foods match by *exact* name (case-insensitive). For anything the parser doesn't find, the skill POSTs a new entry. This means "tbsp" and "tablespoon" are still two different units — the parser helps only for names already in the database, so for a brand-new Mealie install ingredient names will proliferate until the user starts tidying them via merge operations in the web UI.
- **Recipe slugs**: auto-generated by Mealie from the recipe name. They may change if the user renames the recipe in the web UI. The skill's resolvers accept slugs, UUIDs, or title substrings — the latter is what you should use when responding to natural-language requests.
- **API token security**: provided via environment variable only — no credentials are written to disk. Tokens are revocable from the Mealie UI at any time.
- If `python3` cannot find the required packages (e.g. `ModuleNotFoundError`), check for a `.venv` directory in the skill root and use its interpreter instead (e.g. `.venv/bin/python3`).
