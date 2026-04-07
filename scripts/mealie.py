#!/usr/bin/env python3
"""
Mealie Recipe Manager Skill
Requires: httpx, python-dotenv
"""

import datetime as dt
import json
import os
import re
import sys
import uuid
from pathlib import Path

import httpx

from skill_handler import Skill

DEFAULT_TIMEOUT = 30.0

PLAN_TYPES = ["breakfast", "lunch", "dinner", "side", "snack", "drink", "dessert"]

ORGANIZER_ENDPOINTS = {
    "tags":       "/api/organizers/tags",
    "categories": "/api/organizers/categories",
    "tools":      "/api/organizers/tools",
    "foods":      "/api/foods",
    "units":      "/api/units",
    "labels":     "/api/groups/labels",
    "cookbooks":  "/api/households/cookbooks",
}

skill = Skill("mealie", "Mealie Recipe Manager CLI")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    from dotenv import load_dotenv
    load_dotenv()

    env_url = os.environ.get("MEALIE_URL")
    env_token = os.environ.get("MEALIE_API_TOKEN")

    if not env_url or not env_token:
        raise RuntimeError(
            "Missing MEALIE_URL and/or MEALIE_API_TOKEN. "
            "Set them as environment variables or in a .env file."
        )

    return {"base_url": env_url, "api_token": env_token}


# ---------------------------------------------------------------------------
# HTTP client + helpers
# ---------------------------------------------------------------------------

def make_client(config, *, timeout=DEFAULT_TIMEOUT):
    return httpx.Client(
        base_url=config["base_url"],
        headers={
            "Authorization": f"Bearer {config['api_token']}",
            "Accept": "application/json",
        },
        timeout=timeout,
    )


def _http_error(response, *, context):
    status = response.status_code
    if status == 401:
        raise RuntimeError("API token rejected (401).")
    if status == 403:
        raise RuntimeError(f"Forbidden (403) on {context}. Token may lack permission.")
    if status == 404:
        raise RuntimeError(f"Not found (404) on {context}.")
    if status == 422:
        try:
            detail = response.json().get("detail")
            msg = json.dumps(detail) if detail is not None else response.text[:200]
        except Exception:
            msg = response.text[:200]
        raise RuntimeError(f"Validation error on {context}: {msg}")
    if 500 <= status < 600:
        raise RuntimeError(f"Mealie server error ({status}) on {context}.")
    raise RuntimeError(f"Unexpected status {status} on {context}: {response.text[:200]}")


def request(client, method, path, *, context, **kwargs):
    try:
        r = client.request(method, path, **kwargs)
    except httpx.ConnectError as e:
        raise RuntimeError(f"Cannot reach Mealie ({context}): {e}")
    except httpx.TimeoutException as e:
        raise RuntimeError(f"Mealie timed out ({context}): {e}")
    if r.status_code >= 400:
        _http_error(r, context=context)
    return r


def _with_client(fn):
    def wrapper(input):
        config = load_config()
        with make_client(config) as client:
            return fn(input, client, config)
    return wrapper


# ---------------------------------------------------------------------------
# Utils
# ---------------------------------------------------------------------------

_SLUG_NON_WORD = re.compile(r"[^a-z0-9]+")

def slugify(name):
    if not name:
        return ""
    s = name.strip().lower()
    s = _SLUG_NON_WORD.sub("-", s)
    return s.strip("-")


def is_valid_uuid(val):
    try:
        uuid.UUID(str(val))
        return True
    except (ValueError, AttributeError):
        return False


WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

def parse_date_arg(s):
    s = s.strip().lower()
    today = dt.date.today()
    if s == "today":
        return today
    if s == "tomorrow":
        return today + dt.timedelta(days=1)
    if s == "yesterday":
        return today - dt.timedelta(days=1)
    if s in WEEKDAYS:
        target = WEEKDAYS[s]
        delta = (target - today.weekday()) % 7
        return today + dt.timedelta(days=delta)
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        raise RuntimeError(
            f'Invalid date "{s}". Use YYYY-MM-DD, "today", "tomorrow", "yesterday", or a weekday name.'
        )


def read_json_input(source):
    if source == "-":
        text = sys.stdin.read()
    elif source.lstrip().startswith(("{", "[")):
        text = source
    else:
        try:
            text = Path(source).read_text()
        except FileNotFoundError:
            raise RuntimeError(f"File not found: {source}")
        except OSError as e:
            raise RuntimeError(f"Cannot read {source}: {e}")
    if not text.strip():
        raise RuntimeError("Input is empty")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"JSON parse error: {e}")


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------

def resolve_recipe(client, query):
    if not query or not query.strip():
        raise RuntimeError("Recipe argument required")
    q = query.strip()

    try:
        r = client.get(f"/api/recipes/{q.lower()}")
    except httpx.ConnectError as e:
        raise RuntimeError(f"Cannot reach Mealie (resolving recipe {q!r}): {e}")
    except httpx.TimeoutException as e:
        raise RuntimeError(f"Mealie timed out (resolving recipe {q!r}): {e}")
    if r.status_code == 200:
        return r.json()
    if r.status_code == 401:
        _http_error(r, context=f"resolving recipe {q!r}")

    if is_valid_uuid(q):
        try:
            r = request(
                client, "GET", "/api/recipes",
                context=f"recipe lookup by id {q}",
                params={"queryFilter": f'id="{q}"', "perPage": 1},
            )
            data = r.json()
            items = data.get("items") or []
            if items:
                return items[0]
        except RuntimeError:
            pass

    r = request(
        client, "GET", "/api/recipes",
        context=f"recipe search by title {q!r}",
        params={"search": q, "perPage": 10, "orderBy": "name", "orderDirection": "asc"},
    )
    data = r.json()
    matches = data.get("items") or []

    if not matches:
        raise RuntimeError(f'No recipe found matching "{query}"')
    if len(matches) == 1:
        return matches[0]

    exact = [m for m in matches if (m.get("name") or "").lower() == q.lower()]
    if len(exact) == 1:
        return exact[0]

    listing = ", ".join(f'"{m.get("name", "?")}" ({m.get("slug", "?")})' for m in matches[:5])
    suffix = f" (and {len(matches)-5} more)" if len(matches) > 5 else ""
    raise RuntimeError(
        f'Multiple recipes match "{query}": {listing}{suffix}. '
        f"Be more specific or pass the slug."
    )


# ---------------------------------------------------------------------------
# Recipe payload preparation
# ---------------------------------------------------------------------------

def find_or_create_organizer(client, kind, name):
    if not name or not name.strip():
        return None
    endpoint = ORGANIZER_ENDPOINTS[kind]
    needle = name.strip().lower()

    items = fetch_all_pages(client, endpoint, context=f"listing {kind}")
    for item in items:
        if (item.get("name") or "").strip().lower() == needle:
            return item

    r = request(
        client, "POST", endpoint,
        context=f"creating {kind[:-1]} {name!r}",
        json={"name": name.strip()},
    )
    try:
        created = r.json()
    except Exception:
        created = None
    if isinstance(created, dict) and created.get("id"):
        return created

    for item in fetch_all_pages(client, endpoint, context=f"listing {kind}"):
        if (item.get("name") or "").strip().lower() == needle:
            return item
    raise RuntimeError(
        f"Created {kind[:-1]} {name!r} but could not locate it in the list afterwards."
    )


def parse_ingredient_strings(client, strings, parser_name=None):
    if not strings:
        return []
    body = {"ingredients": list(strings)}
    if parser_name:
        body["parser"] = parser_name
    r = request(
        client, "POST", "/api/parser/ingredients",
        context=f"parsing {len(strings)} ingredient string(s)",
        json=body,
    )
    results = r.json() or []
    return [(item or {}).get("ingredient") or {} for item in results]


def _ensure_id(client, kind, obj):
    if not isinstance(obj, dict):
        return obj
    if obj.get("id"):
        return obj
    name = obj.get("name")
    if not name:
        return None
    return find_or_create_organizer(client, kind, name)


def prepare_recipe_payload(client, payload):
    raw_items = payload.get("recipeIngredient") or []
    string_values = [it for it in raw_items if isinstance(it, str)]
    parsed_strings = parse_ingredient_strings(client, string_values)

    new_ings = []
    parsed_iter = iter(parsed_strings)
    for item in raw_items:
        if isinstance(item, str):
            new_ings.append(next(parsed_iter, {}) or {})
        elif isinstance(item, dict):
            new_ings.append(dict(item))

    for ing in new_ings:
        unit = ing.get("unit")
        if isinstance(unit, dict):
            ing["unit"] = _ensure_id(client, "units", unit)
        food = ing.get("food")
        if isinstance(food, dict):
            ing["food"] = _ensure_id(client, "foods", food)
    payload["recipeIngredient"] = new_ings

    for field, kind in [("tags", "tags"), ("recipeCategory", "categories"), ("tools", "tools")]:
        resolved = []
        for t in payload.get(field) or []:
            if isinstance(t, dict):
                if t.get("id"):
                    resolved.append(t)
                    continue
                name = t.get("name")
            elif isinstance(t, str):
                name = t
            else:
                continue
            if name:
                entry = find_or_create_organizer(client, kind, name)
                if entry:
                    resolved.append(entry)
        payload[field] = resolved

    new_steps = []
    for step in payload.get("recipeInstructions") or []:
        if isinstance(step, str):
            step = {"text": step}
        elif not isinstance(step, dict):
            continue
        normalized = dict(step)
        normalized.setdefault("text", "")
        normalized.setdefault("ingredientReferences", [])
        new_steps.append(normalized)
    payload["recipeInstructions"] = new_steps

    new_notes = []
    for note in payload.get("notes") or []:
        if isinstance(note, str):
            note = {"title": "", "text": note}
        elif not isinstance(note, dict):
            continue
        normalized = dict(note)
        normalized.setdefault("title", "")
        normalized.setdefault("text", "")
        new_notes.append(normalized)
    payload["notes"] = new_notes

    return payload


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _fmt_iso_duration(s):
    if not s:
        return None
    m = re.match(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", s)
    if not m:
        return s
    d, h, mm, ss = (int(x) if x else 0 for x in m.groups())
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if mm: parts.append(f"{mm}m")
    if ss and not parts: parts.append(f"{ss}s")
    return "".join(parts) or s


def _ingredient_display(ing):
    if ing.get("display"):
        return ing["display"]
    if ing.get("disableAmount") and ing.get("note"):
        return ing["note"]
    parts = []
    qty = ing.get("quantity")
    if qty is not None and qty != 0:
        parts.append(str(int(qty)) if float(qty).is_integer() else str(qty))
    unit = ing.get("unit") or {}
    if unit.get("name"):
        parts.append(unit["name"])
    food = ing.get("food") or {}
    if food.get("name"):
        parts.append(food["name"])
    line = " ".join(parts)
    if ing.get("note"):
        line = f"{line}, {ing['note']}" if line else ing["note"]
    return line or "(empty)"


def _instruction_text(step):
    return " ".join((step.get("text") or "").split())


def format_recipe_show(recipe, base_url):
    out = []
    name = recipe.get("name") or "(unnamed)"
    out.append(name)

    meta = [("Slug", recipe.get("slug") or "")]
    if recipe.get("orgURL"):
        meta.append(("Source", recipe["orgURL"]))
    if recipe.get("recipeYield"):
        meta.append(("Yields", recipe["recipeYield"]))

    total = _fmt_iso_duration(recipe.get("totalTime"))
    prep = _fmt_iso_duration(recipe.get("prepTime"))
    cook = _fmt_iso_duration(recipe.get("cookTime"))
    if total or prep or cook:
        sub = []
        if prep: sub.append(f"prep {prep}")
        if cook: sub.append(f"cook {cook}")
        primary = total or ""
        if primary and sub:
            meta.append(("Total time", f"{primary}  ({', '.join(sub)})"))
        elif primary:
            meta.append(("Total time", primary))
        elif sub:
            meta.append(("Total time", ", ".join(sub)))

    tags = recipe.get("tags") or []
    if tags:
        meta.append(("Tags", ", ".join(t.get("name", "") for t in tags)))
    cats = recipe.get("recipeCategory") or []
    if cats:
        meta.append(("Categories", ", ".join(c.get("name", "") for c in cats)))
    rating = recipe.get("rating")
    if rating is not None:
        meta.append(("Rating", f"{rating} / 5"))

    for k, v in meta:
        out.append(f"  {k+':':<12} {v}")

    desc = (recipe.get("description") or "").strip()
    if desc:
        out.append("")
        out.append("Description:")
        for line in desc.splitlines():
            out.append(f"  {line.strip()}")

    ings = recipe.get("recipeIngredient") or []
    if ings:
        out.append("")
        out.append(f"Ingredients ({len(ings)}):")
        for ing in ings:
            out.append(f"  - {_ingredient_display(ing)}")

    steps = recipe.get("recipeInstructions") or []
    if steps:
        out.append("")
        out.append(f"Instructions ({len(steps)} steps):")
        for i, step in enumerate(steps, 1):
            out.append(f"  {i}. {_instruction_text(step)}")

    notes = recipe.get("notes") or []
    if notes:
        out.append("")
        out.append("Notes:")
        for n in notes:
            text = (n.get("text") or "").strip()
            title = (n.get("title") or "").strip()
            if title and text:
                out.append(f"  - {title}: {text}")
            elif text:
                out.append(f"  - {text}")

    out.append("")
    out.append(f"URL: {base_url}/recipe/{recipe.get('slug', '')}")
    return "\n".join(out)


def format_recipe_search_row(recipe):
    name = recipe.get("name") or "(unnamed)"
    slug = recipe.get("slug") or "?"
    tags = recipe.get("tags") or []
    tag_str = f"  [{', '.join(t.get('name', '') for t in tags)}]" if tags else ""
    return f"  - {name} ({slug}){tag_str}"


def _entry_label(entry):
    rec = entry.get("recipe")
    if rec and rec.get("name"):
        return f"{rec['name']} ({rec.get('slug', '?')})"
    title = entry.get("title")
    text = entry.get("text")
    if title and text:
        return f"{title} — {text} (free text)"
    if title:
        return f"{title} (free text)"
    if text:
        return f"{text} (free text)"
    return "(empty)"


def format_mealplan_day(date, entries, *, header_prefix="Today's meals"):
    weekday = date.strftime("%A")
    out = [f"{header_prefix} ({weekday}, {date.isoformat()}):"]
    if not entries:
        out.append("  (nothing planned)")
        return "\n".join(out)
    by_type = {}
    for e in entries:
        by_type.setdefault(e.get("entryType", "other"), []).append(e)
    for t in PLAN_TYPES + ["other"]:
        for e in by_type.get(t, []):
            label = _entry_label(e)
            out.append(f"  {t:<10} {label}  [id {e.get('id', '?')}]")
    return "\n".join(out)


def format_mealplan_week(start, days):
    end = start + dt.timedelta(days=6)
    out = [f"Meals for {start.isoformat()} to {end.isoformat()}", ""]
    for i in range(7):
        d = start + dt.timedelta(days=i)
        out.append(f"{d.strftime('%A')} {d.isoformat()}")
        entries = days.get(d.isoformat(), [])
        if not entries:
            out.append("  (nothing planned)")
        else:
            by_type = {}
            for e in entries:
                by_type.setdefault(e.get("entryType", "other"), []).append(e)
            for t in PLAN_TYPES + ["other"]:
                for e in by_type.get(t, []):
                    out.append(f"  {t:<10} {_entry_label(e)}  [id {e.get('id', '?')}]")
        out.append("")
    return "\n".join(out).rstrip()


def format_organizer_list(kind, items):
    out = [f"{kind} ({len(items)}):"]
    if not items:
        out.append("  (empty)")
        return "\n".join(out)
    for item in items:
        name = item.get("name") or item.get("itemId") or "(unnamed)"
        if kind == "units":
            abbr = item.get("abbreviation") or ""
            if abbr and abbr != name:
                out.append(f"  - {name} ({abbr})")
            else:
                out.append(f"  - {name}")
        else:
            out.append(f"  - {name}")
    return "\n".join(out)


def _recipe_summary(recipe, base_url, verb):
    name = recipe.get("name") or "(unnamed)"
    slug = recipe.get("slug") or ""
    ings = recipe.get("recipeIngredient") or []
    steps = recipe.get("recipeInstructions") or []
    tags = recipe.get("tags") or []

    lines = [f'✓ {verb} "{name}"']
    lines.append(f"  Slug:         {slug}")
    lines.append(f"  Ingredients:  {len(ings)}")
    lines.append(f"  Instructions: {len(steps)} steps")
    if tags:
        lines.append(f"  Tags:         {', '.join(t.get('name', '') for t in tags)}")
    if recipe.get("orgURL"):
        lines.append(f"  Source:       {recipe['orgURL']}")
    lines.append(f"  URL:          {base_url}/recipe/{slug}")

    if not ings or not steps:
        print("! Warning: recipe has no ingredients or no instructions.",
              file=sys.stderr)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def fetch_all_pages(client, path, *, params=None, context, max_pages=10, per_page=100):
    params = dict(params or {})
    params.setdefault("perPage", per_page)
    results = []
    page = 1
    while page <= max_pages:
        params["page"] = page
        r = request(client, "GET", path, context=context, params=params)
        data = r.json()
        results.extend(data.get("items") or [])
        if not data.get("next"):
            break
        page += 1
    return results


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@skill.tool("recipe_create",
    description="Create a new recipe from JSON",
    params={
        "source": {"type": "string", "required": True, "cli_positional": True,
                    "description": "Recipe JSON data, or path to JSON file, or '-' for stdin"},
    })
@_with_client
def recipe_create(input, client, config):
    payload = read_json_input(input["source"])
    if not isinstance(payload, dict):
        raise RuntimeError("Recipe JSON must be a JSON object")
    name = (payload.get("name") or "").strip()
    if not name:
        raise RuntimeError("Recipe JSON must have a non-empty 'name' field")

    r = request(client, "GET", "/api/recipes",
                context="checking recipe name uniqueness",
                params={"search": name, "perPage": 10})
    for item in r.json().get("items") or []:
        if (item.get("name") or "").strip().lower() == name.lower():
            slug = item.get("slug")
            raise RuntimeError(
                f'A recipe named "{name}" already exists (slug: {slug}).'
            )

    prepare_recipe_payload(client, payload)

    r = request(client, "POST", "/api/recipes", context="creating recipe",
                json={"name": name})
    slug = r.json()
    if isinstance(slug, dict):
        slug = slug.get("slug") or slug.get("name")
    if not isinstance(slug, str):
        raise RuntimeError(f"Unexpected response from POST /api/recipes: {slug!r}")

    try:
        r = request(client, "GET", f"/api/recipes/{slug}",
                    context=f"loading stub {slug}")
        stub = r.json()
        merged = dict(payload)
        for key in ("id", "slug", "userId", "groupId", "householdId"):
            if stub.get(key) is not None:
                merged[key] = stub[key]

        request(client, "PATCH", f"/api/recipes/{slug}",
                context=f"writing full recipe {slug}", json=merged)
        r = request(client, "GET", f"/api/recipes/{slug}",
                    context=f"reading back {slug}")
        recipe = r.json()
    except BaseException:
        try:
            client.delete(f"/api/recipes/{slug}")
        except Exception:
            pass
        raise

    return _recipe_summary(recipe, config["base_url"], "Created")


@skill.tool("recipe_update",
    description="Update an existing recipe with partial JSON",
    params={
        "recipe": {"type": "string", "required": True, "cli_positional": True,
                    "description": "Slug, id, or title substring"},
        "source": {"type": "string", "required": True, "cli_positional": True,
                    "description": "Partial JSON data, or path to JSON file, or '-' for stdin"},
    })
@_with_client
def recipe_update(input, client, config):
    payload = read_json_input(input["source"])
    if not isinstance(payload, dict):
        raise RuntimeError("Recipe JSON must be a JSON object")
    target = resolve_recipe(client, input["recipe"])
    slug = target["slug"]
    prepare_recipe_payload(client, payload)
    request(client, "PATCH", f"/api/recipes/{slug}",
            context=f"patching recipe {slug}", json=payload)
    r = request(client, "GET", f"/api/recipes/{slug}",
                context=f"reading back {slug}")
    return _recipe_summary(r.json(), config["base_url"], "Updated")


@skill.tool("recipe_search",
    description="Search recipes by name, tag, category, or cookbook",
    params={
        "query":    {"type": "string", "cli_positional": True,
                     "description": "Substring of recipe name (optional)"},
        "tag":      {"type": "array", "items": {"type": "string"},
                     "description": "Filter by tags (AND semantics)"},
        "category": {"type": "array", "items": {"type": "string"},
                     "description": "Filter by categories (AND semantics)"},
        "cookbook":  {"type": "string", "description": "Filter by cookbook name"},
        "limit":    {"type": "integer", "description": "Max results (default 20)",
                     "default": 20},
    })
@_with_client
def recipe_search(input, client, config):
    params = {
        "perPage": input.get("limit", 20),
        "orderBy": "name",
        "orderDirection": "asc",
    }
    if input.get("query"):
        params["search"] = input["query"]
    if input.get("tag"):
        params["tags"] = ",".join(input["tag"])
        params["requireAllTags"] = "true"
    if input.get("category"):
        params["categories"] = ",".join(input["category"])
        params["requireAllCategories"] = "true"
    if input.get("cookbook"):
        params["cookbook"] = input["cookbook"]

    r = request(client, "GET", "/api/recipes", context="searching recipes",
                params=params)
    data = r.json()
    items = data.get("items") or []
    total = data.get("total", len(items))

    label = input.get("query") or "your filters"
    if not items:
        return f'No recipes found matching "{label}".'
    shown = f" (showing first {len(items)} of {total})" if total > len(items) else ""
    lines = [f'Found {len(items)} recipes matching "{label}"{shown}:']
    for recipe in items:
        lines.append(format_recipe_search_row(recipe))
    return "\n".join(lines)


@skill.tool("recipe_show",
    description="Show one recipe",
    params={
        "recipe": {"type": "string", "required": True, "cli_positional": True,
                    "description": "Slug, id, or title substring"},
        "json":   {"type": "boolean",
                    "description": "Emit raw recipe JSON instead of formatted text"},
    })
@_with_client
def recipe_show(input, client, config):
    recipe = resolve_recipe(client, input["recipe"])
    if recipe.get("slug"):
        r = request(client, "GET", f"/api/recipes/{recipe['slug']}",
                    context=f"loading recipe {recipe['slug']}")
        recipe = r.json()
    if input.get("json"):
        return json.dumps(recipe, indent=2, sort_keys=True)
    return format_recipe_show(recipe, config["base_url"])


@skill.tool("recipe_random",
    description="Pick a random recipe (optionally filtered)",
    params={
        "tag":      {"type": "array", "items": {"type": "string"},
                     "description": "Filter by tags"},
        "category": {"type": "array", "items": {"type": "string"},
                     "description": "Filter by categories"},
    })
@_with_client
def recipe_random(input, client, config):
    params = {
        "perPage": 1,
        "orderBy": "random",
        "orderDirection": "asc",
        "paginationSeed": uuid.uuid4().hex,
    }
    if input.get("tag"):
        params["tags"] = ",".join(input["tag"])
        params["requireAllTags"] = "true"
    if input.get("category"):
        params["categories"] = ",".join(input["category"])
        params["requireAllCategories"] = "true"

    r = request(client, "GET", "/api/recipes", context="random recipe", params=params)
    items = r.json().get("items") or []

    if not items:
        params.pop("orderBy", None)
        params.pop("paginationSeed", None)
        params["perPage"] = 100
        r = request(client, "GET", "/api/recipes", context="random recipe (fallback)",
                    params=params)
        items = r.json().get("items") or []
        if items:
            import random
            items = [random.choice(items)]

    if not items:
        return "No recipes match the given filters."

    recipe = items[0]
    if recipe.get("slug"):
        r = request(client, "GET", f"/api/recipes/{recipe['slug']}",
                    context=f"loading random recipe {recipe['slug']}")
        recipe = r.json()
    return format_recipe_show(recipe, config["base_url"])


@skill.tool("recipe_delete",
    description="Permanently delete a recipe",
    params={
        "recipe": {"type": "string", "required": True, "cli_positional": True,
                    "description": "Slug, id, or title substring"},
        "yes":    {"type": "boolean", "description": "Confirm deletion"},
    })
@_with_client
def recipe_delete(input, client, config):
    target = resolve_recipe(client, input["recipe"])
    slug = target["slug"]
    name = target.get("name") or slug
    if not input.get("yes"):
        raise RuntimeError(
            f'This will permanently delete "{name}" ({slug}). Pass yes=true to confirm.'
        )
    request(client, "DELETE", f"/api/recipes/{slug}",
            context=f"deleting recipe {slug}")
    return f'✓ Deleted "{name}" ({slug})'


@skill.tool("recipe_parse_ingredients",
    description="Parse freeform ingredient strings via Mealie's parser",
    params={
        "strings": {"type": "array", "items": {"type": "string"}, "required": True,
                     "cli_positional": True,
                     "description": 'Ingredient strings, e.g. "2 cups flour"'},
        "parser":  {"type": "string", "enum": ["nlp", "brute", "openai"],
                     "description": "Parser to use (server default if omitted)"},
    })
@_with_client
def recipe_parse_ingredients(input, client, config):
    strings = input.get("strings") or []
    if not strings:
        raise RuntimeError("No ingredient strings provided.")
    parsed = parse_ingredient_strings(client, strings, parser_name=input.get("parser"))
    return json.dumps(parsed, indent=2, sort_keys=True)


@skill.tool("mealplan_today",
    description="Show today's planned meals",
    params={})
@_with_client
def mealplan_today(input, client, config):
    r = request(client, "GET", "/api/households/mealplans/today",
                context="loading today's mealplans")
    return format_mealplan_day(dt.date.today(), r.json() or [])


@skill.tool("mealplan_week",
    description="Show a 7-day meal plan window",
    params={
        "start": {"type": "string",
                   "description": "Start date (YYYY-MM-DD, today, tomorrow, or weekday name)"},
    })
@_with_client
def mealplan_week(input, client, config):
    start = parse_date_arg(input["start"]) if input.get("start") else dt.date.today()
    end = start + dt.timedelta(days=6)
    items = fetch_all_pages(
        client, "/api/households/mealplans",
        params={
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "orderBy": "date",
            "orderDirection": "asc",
        },
        context="loading week of mealplans",
    )
    days = {}
    for e in items:
        d = e.get("date")
        if d:
            days.setdefault(d, []).append(e)
    return format_mealplan_week(start, days)


@skill.tool("mealplan_add",
    description="Add a meal plan entry",
    params={
        "date":   {"type": "string", "required": True, "cli_positional": True,
                    "description": "Date (YYYY-MM-DD, today, tomorrow, or weekday name)"},
        "type":   {"type": "string", "required": True, "cli_positional": True,
                    "enum": PLAN_TYPES, "description": "Meal type"},
        "recipe": {"type": "string", "cli_positional": True,
                    "description": "Recipe slug/title (omit if using --title/--text)"},
        "title":  {"type": "string", "description": "Free-text entry title"},
        "text":   {"type": "string", "description": "Free-text entry body"},
    })
@_with_client
def mealplan_add(input, client, config):
    if input.get("recipe") and (input.get("title") or input.get("text")):
        raise RuntimeError("Specify either a recipe or --title/--text, not both")

    date = parse_date_arg(input["date"])
    body = {
        "date": date.isoformat(),
        "entryType": input["type"],
        "title": input.get("title") or "",
        "text": input.get("text") or "",
        "recipeId": None,
    }

    if input.get("recipe"):
        recipe = resolve_recipe(client, input["recipe"])
        body["recipeId"] = recipe["id"]
        body["title"] = ""
        body["text"] = ""

    r = request(client, "POST", "/api/households/mealplans",
                context="creating mealplan entry", json=body)
    entry = r.json()
    return f'✓ Added {input["type"]} for {date.isoformat()}: {_entry_label(entry)}  [id {entry.get("id")}]'


@skill.tool("mealplan_random",
    description="Add a random recipe to the meal plan",
    params={
        "date": {"type": "string", "required": True, "cli_positional": True,
                  "description": "Date (YYYY-MM-DD, today, tomorrow, or weekday name)"},
        "type": {"type": "string", "required": True, "cli_positional": True,
                  "enum": PLAN_TYPES, "description": "Meal type"},
    })
@_with_client
def mealplan_random(input, client, config):
    date = parse_date_arg(input["date"])
    body = {"date": date.isoformat(), "entryType": input["type"]}
    r = request(client, "POST", "/api/households/mealplans/random",
                context="creating random mealplan entry", json=body)
    entry = r.json()
    return f'✓ Added random {input["type"]} for {date.isoformat()}: {_entry_label(entry)}  [id {entry.get("id")}]'


@skill.tool("mealplan_delete",
    description="Delete a meal plan entry by its numeric ID",
    params={
        "id": {"type": "integer", "required": True, "cli_positional": True,
                "description": "Meal plan entry ID (from mealplan today/week output)"},
    })
@_with_client
def mealplan_delete(input, client, config):
    entry_id = input["id"]
    request(client, "DELETE", f"/api/households/mealplans/{entry_id}",
            context=f"deleting mealplan entry {entry_id}")
    return f"✓ Deleted mealplan entry {entry_id}"


@skill.tool("organizers_list",
    description="List all entries for an organizer kind",
    params={
        "kind":  {"type": "string", "required": True, "cli_positional": True,
                   "enum": list(ORGANIZER_ENDPOINTS),
                   "description": "Organizer type to list"},
        "limit": {"type": "integer",
                   "description": "Maximum entries to return"},
    })
@_with_client
def organizers_list(input, client, config):
    kind = input["kind"]
    endpoint = ORGANIZER_ENDPOINTS[kind]
    items = fetch_all_pages(client, endpoint, context=f"listing {kind}")
    if input.get("limit") is not None:
        items = items[:input["limit"]]
    return format_organizer_list(kind, items)


if __name__ == "__main__":
    skill.run()
