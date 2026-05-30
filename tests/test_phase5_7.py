"""통합 테스트 — 새 모듈 4개 검증"""
import sys
sys.path.insert(0, "src")

from collector.api_collector import OpenAPICollector, GraphQLCollector, RESTAPICollector
from collector.notifier import NotificationManager, DriftEvent, Severity
from api.auth import UserStore, JWTManager, RBACMiddleware, Role
from api.server import app

passed = 0
failed = 0


def test(name, fn):
    global passed, failed
    try:
        fn()
        passed += 1
        print(f"  ✅ {name}")
    except Exception as e:
        failed += 1
        print(f"  ❌ {name}: {e}")


# ── OpenAPI ─────────────────────────────────────────
def test_openapi():
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "TestAPI", "version": "1.0"},
        "paths": {
            "/users": {
                "get": {"summary": "List users", "tags": ["Users"],
                        "responses": {"200": {"description": "OK"}}},
                "post": {"summary": "Create user", "tags": ["Users"],
                         "responses": {"201": {"description": "Created"}}},
            },
            "/orders": {
                "get": {"summary": "List orders", "tags": ["Orders"],
                        "responses": {"200": {"description": "OK"}}},
            },
        },
    }
    col = OpenAPICollector()
    schema = col.collect_from_dict(spec)
    assert len(schema.endpoints) == 3, f"Expected 3, got {len(schema.endpoints)}"
    tables = col.endpoints_to_table_schema(schema)
    assert len(tables) == 2, f"Expected 2 tables, got {len(tables)}"


def test_graphql_import():
    gc = GraphQLCollector()
    assert gc.INTROSPECTION_QUERY is not None


def test_rest_import():
    rc = RESTAPICollector()
    assert rc.max_sample_size == 100


# ── Notifier ────────────────────────────────────────
def test_notifier_empty():
    mgr = NotificationManager()
    assert mgr.channel_count == 0
    result = mgr.notify_drift(DriftEvent(
        event_id="t1", timestamp="2025-01-01T00:00:00",
        severity=Severity.WARNING, db_name="test", table_name="users",
        change_type="column_added", field_name="email",
    ))
    assert result == {}
    assert len(mgr.history) == 1


# ── RBAC ────────────────────────────────────────────
def test_user_store():
    us = UserStore()
    u = us.create_user("alice", "a@test.com", "pass123", Role.ADMIN)
    assert u.role == Role.ADMIN
    assert u.api_key.startswith("onto_")
    auth = us.authenticate("alice", "pass123")
    assert auth is not None
    assert auth.username == "alice"
    bad = us.authenticate("alice", "wrong")
    assert bad is None


def test_jwt():
    us = UserStore()
    jwt = JWTManager("my-secret")
    u = us.create_user("bob", "b@test.com", "pw", Role.VIEWER)
    token = jwt.create_token(u)
    payload = jwt.verify_token(token, us)
    assert payload is not None
    assert payload.username == "bob"
    assert payload.role == "viewer"


def test_rbac_permissions():
    us = UserStore()
    jwt = JWTManager("secret")
    rbac = RBACMiddleware(us, jwt)
    admin = us.create_user("adm", "a@t.com", "pw", Role.ADMIN)
    viewer = us.create_user("vwr", "v@t.com", "pw", Role.VIEWER)
    rbac.require_permission(admin, "user:manage")
    rbac.require_permission(viewer, "graph:read")
    try:
        rbac.require_permission(viewer, "user:manage")
        assert False, "Should have raised"
    except PermissionError:
        pass


# ── FastAPI Routes ──────────────────────────────────
def test_api_routes():
    routes = [r.path for r in app.routes if hasattr(r, "path")]
    api_routes = [r for r in routes if r.startswith("/api")]
    assert len(api_routes) >= 10, f"Expected 10+ API routes, got {len(api_routes)}"
    auth_routes = [r for r in api_routes if "auth" in r]
    assert len(auth_routes) >= 3, f"Expected 3+ auth routes, got {len(auth_routes)}"


# ── 실행 ────────────────────────────────────────────
print("Running DB Ontology Phase 5-7 tests...\n")

test("OpenAPI Collector", test_openapi)
test("GraphQL Collector", test_graphql_import)
test("REST Collector", test_rest_import)
test("Notifier (no channels)", test_notifier_empty)
test("User Store + Auth", test_user_store)
test("JWT Issue & Verify", test_jwt)
test("RBAC Permission Check", test_rbac_permissions)
test("FastAPI Routes", test_api_routes)

print(f"\nResults: {passed} passed, {failed} failed out of {passed + failed}")
if failed == 0:
    print("ALL TESTS PASSED")
else:
    print("SOME TESTS FAILED")
    sys.exit(1)
