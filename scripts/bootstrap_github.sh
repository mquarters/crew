#!/usr/bin/env bash
#
# Bootstrap the crew's GitHub substrate: repos, the Projects v2 board, its
# status columns, custom fields, labels, and branch protection.
#
# Idempotent where the API allows it: re-running skips what already exists.
#
# Prerequisites:
#   gh auth login
#   gh auth refresh -s project,read:project   # Projects v2 is NOT in default scopes
#
set -euo pipefail

CREW_REPO="${CREW_REPO:-crew}"
PILOT_REPO="${PILOT_REPO:-sprint-metrics}"
PROJECT_TITLE="${PROJECT_TITLE:-Crew Delivery}"
VISIBILITY="${VISIBILITY:-private}"

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m  ! \033[0m%s\n' "$*"; }
ok()   { printf '\033[32m  ✓ \033[0m%s\n' "$*"; }

# --- preflight -----------------------------------------------------------
say "Preflight"
command -v gh >/dev/null || { echo "gh CLI not found"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Not logged in. Run: gh auth login"; exit 1; }

if ! gh auth status 2>&1 | grep -q "'project'"; then
  warn "The 'project' scope is missing — Projects v2 calls will fail."
  warn "Run: gh auth refresh -s project,read:project"
  exit 1
fi
OWNER="$(gh api user --jq .login)"
ok "authenticated as ${OWNER} with project scope"

# --- repositories --------------------------------------------------------
say "Repositories"
for repo in "$CREW_REPO" "$PILOT_REPO"; do
  if gh repo view "${OWNER}/${repo}" >/dev/null 2>&1; then
    ok "${repo} already exists"
  else
    gh repo create "${OWNER}/${repo}" "--${VISIBILITY}" \
      --description "Part of the crew agent organization" >/dev/null
    ok "created ${repo}"
  fi
done

# --- project board -------------------------------------------------------
say "Project board"
PROJECT_NUMBER="$(gh project list --owner "$OWNER" --format json \
  --jq ".projects[] | select(.title==\"${PROJECT_TITLE}\") | .number" 2>/dev/null || true)"

if [[ -z "$PROJECT_NUMBER" ]]; then
  PROJECT_NUMBER="$(gh project create --owner "$OWNER" --title "$PROJECT_TITLE" \
    --format json --jq .number)"
  ok "created project #${PROJECT_NUMBER}"
else
  ok "project #${PROJECT_NUMBER} already exists"
fi

PROJECT_ID="$(gh project view "$PROJECT_NUMBER" --owner "$OWNER" --format json --jq .id)"

# --- status columns ------------------------------------------------------
# The built-in Status field ships with Todo/In Progress/Done. Its options are
# replaced wholesale via GraphQL; `gh project field-create` cannot edit it.
say "Status columns"
STATUS_FIELD_ID="$(gh project field-list "$PROJECT_NUMBER" --owner "$OWNER" --format json \
  --jq '.fields[] | select(.name=="Status") | .id')"

read -r -d '' STATUS_OPTIONS <<'JSON' || true
[
  {"name":"Inbox (Goals)",    "color":"GRAY",   "description":"Sponsor goals awaiting epic proposal"},
  {"name":"Needs Refinement", "color":"ORANGE", "description":"Not yet meeting Definition of Ready"},
  {"name":"Ready",            "color":"YELLOW", "description":"Meets Definition of Ready"},
  {"name":"Sprint Backlog",   "color":"BLUE",   "description":"Admitted to the current sprint"},
  {"name":"In Progress",      "color":"PURPLE", "description":"Being implemented"},
  {"name":"In Review",        "color":"PINK",   "description":"PR open, awaiting review"},
  {"name":"QA",               "color":"RED",    "description":"Verifying against acceptance criteria"},
  {"name":"Done",             "color":"GREEN",  "description":"Merged and green"},
  {"name":"Blocked",          "color":"GRAY",   "description":"Blocked on an external input"}
]
JSON

gh api graphql -f query='
  mutation($field: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]!) {
    updateProjectV2Field(input: {fieldId: $field, singleSelectOptions: $options}) {
      projectV2Field { ... on ProjectV2SingleSelectField { id name } }
    }
  }' -f field="$STATUS_FIELD_ID" -f options="$STATUS_OPTIONS" >/dev/null
ok "status columns set"

# --- custom fields -------------------------------------------------------
say "Custom fields"
create_field() {
  local name="$1" type="$2" opts="${3:-}"
  if gh project field-list "$PROJECT_NUMBER" --owner "$OWNER" --format json \
      --jq ".fields[] | select(.name==\"${name}\") | .id" | grep -q .; then
    ok "${name} already exists"; return
  fi
  if [[ -n "$opts" ]]; then
    gh project field-create "$PROJECT_NUMBER" --owner "$OWNER" \
      --name "$name" --data-type "$type" --single-select-options "$opts" >/dev/null
  else
    gh project field-create "$PROJECT_NUMBER" --owner "$OWNER" \
      --name "$name" --data-type "$type" >/dev/null
  fi
  ok "created ${name}"
}

create_field "Type"        SINGLE_SELECT "Epic,Story,Task,Bug,Spike"
create_field "Priority"    SINGLE_SELECT "P0,P1,P2,P3"
create_field "Owner Agent" SINGLE_SELECT "Product Owner,Business Analyst,Architect,Developer,QA Engineer,Code Reviewer,Scrum Master,Tech Writer"
create_field "Points"      NUMBER
create_field "Escalated"   NUMBER

# --- labels --------------------------------------------------------------
say "Labels"
add_label() {
  local repo="$1" name="$2" color="$3" desc="$4"
  gh label create "$name" --repo "${OWNER}/${repo}" --color "$color" \
    --description "$desc" --force >/dev/null 2>&1 && ok "${repo}: ${name}"
}
for repo in "$CREW_REPO" "$PILOT_REPO"; do
  add_label "$repo" "crew:refine"     "0E8A16" "Routed to the refinement crew"
  add_label "$repo" "crew:dev"        "1D76DB" "Routed to the delivery crew"
  add_label "$repo" "crew:qa"         "5319E7" "Routed to QA"
  add_label "$repo" "crew:review"     "B60205" "Routed to code review"
  add_label "$repo" "needs:human"     "D93F0B" "Awaiting the Product Sponsor"
  add_label "$repo" "blocked"         "000000" "Blocked on an external input"
  add_label "$repo" "defect:prompt"   "FBCA04" "Agent prompt or schema defect"
  add_label "$repo" "defect:process"  "FEF2C0" "Process defect raised by a retro"
done
add_label "$CREW_REPO" "escalation:rate-limited" "C5DEF5" "Parked on a Claude usage limit"

# --- branch protection ---------------------------------------------------
# Agents never push to main. Everything lands through a reviewed PR.
say "Branch protection"
for repo in "$CREW_REPO" "$PILOT_REPO"; do
  if gh api -X PUT "repos/${OWNER}/${repo}/branches/main/protection" \
      -H "Accept: application/vnd.github+json" \
      -F "required_pull_request_reviews[required_approving_review_count]=1" \
      -F "required_status_checks[strict]=true" \
      -F "required_status_checks[contexts][]=tests" \
      -F "enforce_admins=false" \
      -F "restrictions=" >/dev/null 2>&1; then
    ok "${repo}: main protected"
  else
    warn "${repo}: could not set protection (needs at least one commit on main, and"
    warn "  private-repo protection requires a paid plan). Re-run after the first push."
  fi
done

say "Done"
echo
echo "  Project:  https://github.com/users/${OWNER}/projects/${PROJECT_NUMBER}"
echo "  Crew:     https://github.com/${OWNER}/${CREW_REPO}"
echo "  Pilot:    https://github.com/${OWNER}/${PILOT_REPO}"
echo
echo "  Record these in .env:"
echo "    GITHUB_OWNER=${OWNER}"
echo "    GITHUB_PROJECT_NUMBER=${PROJECT_NUMBER}"
echo "    GITHUB_PROJECT_ID=${PROJECT_ID}"
