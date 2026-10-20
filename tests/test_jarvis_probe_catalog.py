"""Validate each catalogued probe against vulnerable and fixed code shapes.

Every probe must fire on the vulnerable shape and go silent on the fixed shape.
Fixtures cover tenant scoping, proxy trust, pricing, admin access, and navigation.
"""

import pytest

from argus.jarvis import probes

CYPHER_BAD = '''\
async def execute_cypher(req, user=Depends(get_current_user)):
    with driver.session() as session:
        result = session.run(query, **(req.params or {}))
        return _serialize(result)
'''
CYPHER_GOOD = '''\
async def execute_cypher(req, user=Depends(get_current_user)):
    scoped_query = scope_cypher_to_user(query)
    params = dict(req.params or {})
    params[USER_ID_PARAM] = str(user.id)
    with driver.session() as session:
        result = session.run(scoped_query, **params)
        return _serialize(result)
'''

NGINX_BAD = '''\
geo $allowed { default 0; 192.168.0.0/16 1; 127.0.0.1 1; }
server {
    if ($allowed = 0) { return 403; }
    allow 192.168.0.0/16;
    deny all;
}
'''
NGINX_GOOD = '''\
set_real_ip_from 192.168.0.0/16;
real_ip_header CF-Connecting-IP;
real_ip_recursive on;
geo $allowed { default 0; 192.168.0.0/16 1; 127.0.0.1 1; }
server {
    if ($allowed = 0) { return 403; }
    allow 192.168.0.0/16;
    deny all;
}
'''

PRICE_BAD = '''\
const itemSchema = z.object({
  productSlug: z.string(),
  quantity: z.number().int().positive(),
  unitPriceToman: z.number().int().nonnegative(),
});
export async function POST(req) {
  const { items } = schema.parse(await req.json());
  const order = await createOrder({ items });
  return Response.json(order);
}
'''
PRICE_GOOD = '''\
const itemSchema = z.object({
  productSlug: z.string(),
  quantity: z.number().int().positive(),
  unitPriceToman: z.number().int().nonnegative(),
});
export async function POST(req) {
  const fetched = await Promise.all(slugs.map((s) => getProductBySlug(s)));
  for (const item of data.items) {
    const product = productBySlug.get(item.productSlug);
    pricedItems.push({ unitPriceToman: product.price_toman });
  }
}
'''

ADMIN_BAD = '''\
export default async function AdminOverview({ params }) {
  const summary = await fetchOverviewCounts();
  const orders = await db.query("SELECT * FROM orders");
  return <Dash orders={orders} summary={summary} />;
}
'''
ADMIN_GOOD = '''\
export default async function AdminOverview({ params }) {
  await requireAdmin();
  const summary = await fetchOverviewCounts();
  return <Dash summary={summary} />;
}
'''

SW_BAD = '''\
self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  e.respondWith(caches.match(e.request));
});
'''
SW_GOOD = '''\
self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  if (e.request.mode === 'navigate') return;
  e.respondWith(caches.match(e.request));
});
'''

BIND_BAD = '''\
services:
  api:
    command: sh -c "uvicorn example.api.app:app --host 0.0.0.0 --port 8000"
'''
BIND_GOOD = '''\
services:
  api:
    command: sh -c "uvicorn example.api.app:app --host 127.0.0.1 --port 8000"
'''

# (probe_id, glob-matching relative path, known-bad code, known-good fix)
CASES = [
    ("unscoped_cypher_endpoint", "example/api/v1/cypher.py", CYPHER_BAD, CYPHER_GOOD),
    ("rfc1918_gate_trust", "deploy/nginx/nginx.conf", NGINX_BAD, NGINX_GOOD),
    ("client_trusted_price", "app/api/checkout/place/route.ts", PRICE_BAD, PRICE_GOOD),
    ("unguarded_admin_route_pii", "app/admin/page.tsx", ADMIN_BAD, ADMIN_GOOD),
    ("service_worker_navigation_intercept", "pwa/sw.js", SW_BAD, SW_GOOD),
    ("app_bound_to_all_interfaces", "docker-compose.dev.yml", BIND_BAD, BIND_GOOD),
]


def _probe(probe_id: str):
    return next(p for p in probes.INCIDENT_PROBES if p.probe_id == probe_id)


def _write(root, rel: str, text: str) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return str(root)


def test_catalog_has_the_six_validated_probes():
    ids = {p.probe_id for p in probes.INCIDENT_PROBES}
    assert len(probes.INCIDENT_PROBES) == 6
    assert {c[0] for c in CASES} == ids


@pytest.mark.parametrize("probe_id,rel,bad,good", CASES, ids=[c[0] for c in CASES])
def test_probe_fires_on_real_bad_and_silent_on_real_fix(tmp_path, probe_id, rel, bad, good):
    probe = _probe(probe_id)
    bad_root = _write(tmp_path / "bad", rel, bad)
    good_root = _write(tmp_path / "good", rel, good)
    assert probes.run_probe(probe, bad_root), f"{probe_id} must fire on the known-bad code"
    assert probes.run_probe(probe, good_root) == [], f"{probe_id} must be silent on the known-good fix"


# --- precision: real false positives seen in the first live scan must not fire ---

def test_app_bind_ignores_db_urls_and_env_values(tmp_path):
    probe = _probe("app_bound_to_all_interfaces")
    root = _write(tmp_path, "docker-compose.yml",
                  'services:\n  db:\n    environment:\n'
                  '      DATABASE_URL: "postgresql+asyncpg://u:p@0.0.0.0:5432/db"\n'
                  '      OAUTH2_PROXY_HTTP_ADDRESS: "0.0.0.0:4180"\n')
    assert probes.run_probe(probe, root) == []


def test_app_bind_catches_host_port_map_and_host_flag(tmp_path):
    probe = _probe("app_bound_to_all_interfaces")
    mapped = _write(tmp_path / "m", "docker-compose.yml",
                    'services:\n  api:\n    ports:\n      - "0.0.0.0:8000:8000"\n')
    flagged = _write(tmp_path / "h", "docker-compose.yml",
                     'command: uvicorn app:app --host 0.0.0.0 --port 8000\n')
    assert probes.run_probe(probe, mapped)
    assert probes.run_probe(probe, flagged)


def test_client_price_is_scoped_to_checkout_not_any_api_file(tmp_path):
    probe = _probe("client_trusted_price")
    root = _write(tmp_path, "api/scripts/event-scraper/schemas.ts",
                  "const s = z.object({ priceMin: z.number().int() });\n")
    assert probes.run_probe(probe, root) == []


# Different applications can use inline query scoping or asset-only service
# worker handling. Both guards must prevent false positives.
CYPHER_GOOD_INLINE = '''\
async def execute_cypher(req, user=Depends(get_current_user)):
    query = "MATCH (n:KGNode {user_id: $user_id}) RETURN n"
    with driver.session() as session:
        result = session.run(query, user_id=str(user.id))
        return _serialize(result)
'''
SW_GOOD_ASSET = '''\
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.headers.get('accept') === 'text/event-stream') return;
  if (isAsset(url.pathname)) {
    e.respondWith(caches.match(e.request));
  }
});
'''


def test_probes_recognise_valid_cross_repo_guard_variants(tmp_path):
    cypher = _probe("unscoped_cypher_endpoint")
    sw = _probe("service_worker_navigation_intercept")
    cypher_root = _write(tmp_path / "c", "example/api/v1/cypher.py", CYPHER_GOOD_INLINE)
    sw_root = _write(tmp_path / "s", "pwa/sw.js", SW_GOOD_ASSET)
    assert probes.run_probe(cypher, cypher_root) == [], "inline user_id:$user_id is a valid scope"
    assert probes.run_probe(sw, sw_root) == [], "isAsset gating is a valid navigation guard"
