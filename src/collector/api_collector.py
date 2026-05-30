"""
Phase 2-4: API-based Schema Collector
REST API, GraphQL, OpenAPI 등에서 스키마 정보를 수집하는 어댑터
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)


@dataclass
class APIEndpoint:
    """API 엔드포인트 메타데이터"""
    path: str
    method: str
    summary: str = ""
    description: str = ""
    parameters: list[dict] = field(default_factory=list)
    response_schema: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)


@dataclass
class APISchema:
    """수집된 API 스키마"""
    source_url: str
    schema_type: str  # "rest", "graphql", "openapi", "grpc"
    title: str = ""
    version: str = ""
    endpoints: list[APIEndpoint] = field(default_factory=list)
    definitions: dict = field(default_factory=dict)
    raw_schema: dict = field(default_factory=dict)


class OpenAPICollector:
    """OpenAPI/Swagger 스펙에서 스키마 수집"""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def collect_from_url(self, spec_url: str) -> APISchema:
        """URL에서 OpenAPI 스펙을 가져와 파싱"""
        try:
            resp = httpx.get(spec_url, timeout=self.timeout)
            resp.raise_for_status()
            raw = resp.json()
            return self._parse_openapi(raw, spec_url)
        except Exception as e:
            logger.error(f"OpenAPI spec fetch failed for {spec_url}: {e}")
            return APISchema(source_url=spec_url, schema_type="openapi")

    def collect_from_file(self, file_path: str) -> APISchema:
        """로컬 파일에서 OpenAPI 스펙 파싱"""
        with open(file_path, "r") as f:
            raw = json.load(f)
        return self._parse_openapi(raw, file_path)

    def collect_from_dict(self, spec: dict, source: str = "inline") -> APISchema:
        """딕셔너리에서 직접 파싱"""
        return self._parse_openapi(spec, source)

    def _parse_openapi(self, raw: dict, source_url: str) -> APISchema:
        spec_version = raw.get("openapi", raw.get("swagger", "3.0.0"))
        info = raw.get("info", {})
        paths = raw.get("paths", {})
        components = raw.get("components", {}).get("schemas", raw.get("definitions", {}))

        endpoints: list[APIEndpoint] = []
        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method, detail in methods.items():
                if method.upper() not in ("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
                    continue
                if not isinstance(detail, dict):
                    continue
                params = detail.get("parameters", [])
                # request_body parameters 병합 (OpenAPI 3.x)
                rb = detail.get("requestBody", {})
                if rb:
                    content = rb.get("content", {})
                    for media_type, media_obj in content.items():
                        if isinstance(media_obj, dict) and "schema" in media_obj:
                            params.append({
                                "in": "body",
                                "name": "body",
                                "schema": media_obj["schema"],
                            })
                            break

                response_schema = {}
                responses = detail.get("responses", {})
                for code in ("200", "201"):
                    if code in responses:
                        resp_obj = responses[code]
                        content = resp_obj.get("content", {})
                        for media_type, media_obj in content.items():
                            if isinstance(media_obj, dict):
                                response_schema = media_obj.get("schema", {})
                                break
                        if response_schema:
                            break

                endpoints.append(APIEndpoint(
                    path=path,
                    method=method.upper(),
                    summary=detail.get("summary", ""),
                    description=detail.get("description", ""),
                    parameters=params,
                    response_schema=response_schema,
                    tags=detail.get("tags", []),
                ))

        return APISchema(
            source_url=source_url,
            schema_type="openapi",
            title=info.get("title", ""),
            version=info.get("version", ""),
            endpoints=endpoints,
            definitions=components,
            raw_schema=raw,
        )

    def endpoints_to_table_schema(self, api_schema: APISchema) -> list[dict]:
        """수집된 API 엔드포인트를 DB 테이블 스키마처럼 변환"""
        tables = []
        # 태그별로 테이블 그룹화
        tag_groups: dict[str, list[APIEndpoint]] = {}
        untagged: list[APIEndpoint] = []

        for ep in api_schema.endpoints:
            if ep.tags:
                for tag in ep.tags:
                    tag_groups.setdefault(tag, []).append(ep)
            else:
                untagged.append(ep)

        for tag, eps in tag_groups.items():
            columns = []
            for ep in eps:
                col_type = self._infer_type_from_response(ep.response_schema)
                columns.append({
                    "column_name": f"{ep.method}_{ep.path.replace('/', '_').strip('_')}",
                    "data_type": col_type,
                    "is_nullable": True,
                    "default_value": None,
                    "table_name": tag,
                    "source": "api",
                })
            tables.append({
                "table_name": tag,
                "schema_name": f"api_{api_schema.title or 'unknown'}".lower().replace(" ", "_"),
                "columns": columns,
                "source": "openapi",
            })

        if untagged:
            columns = []
            for ep in untagged:
                col_type = self._infer_type_from_response(ep.response_schema)
                columns.append({
                    "column_name": f"{ep.method}_{ep.path.replace('/', '_').strip('_')}",
                    "data_type": col_type,
                    "is_nullable": True,
                    "default_value": None,
                    "table_name": "default",
                    "source": "api",
                })
            tables.append({
                "table_name": "default",
                "schema_name": f"api_{api_schema.title or 'unknown'}".lower().replace(" ", "_"),
                "columns": columns,
                "source": "openapi",
            })

        return tables

    @staticmethod
    def _infer_type_from_response(schema: dict) -> str:
        if not schema:
            return "object"
        t = schema.get("type", "object")
        if t == "array":
            items = schema.get("items", {})
            item_type = items.get("type", "object")
            return f"array<{item_type}>"
        return t


class GraphQLCollector:
    """GraphQL 스키마 수집 (introspection query)"""

    INTROSPECTION_QUERY = """
    query IntrospectionQuery {
      __schema {
        queryType { name }
        mutationType { name }
        subscriptionType { name }
        types {
          name
          kind
          description
          fields(includeDeprecated: true) {
            name
            description
            args { name type { name kind ofType { name kind } } }
            type { name kind ofType { name kind ofType { name kind } } }
            isDeprecated
          }
          inputFields { name type { name kind ofType { name kind } } }
          interfaces { name kind }
          enumValues { name }
          possibleTypes { name kind }
        }
        directives { name description locations args { name type { name kind } } }
      }
    }
    """

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def collect(self, endpoint_url: str, headers: Optional[dict] = None) -> APISchema:
        """GraphQL introspection으로 스키마 수집"""
        try:
            resp = httpx.post(
                endpoint_url,
                json={"query": self.INTROSPECTION_QUERY},
                headers=headers or {"Content-Type": "application/json"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data.get("data", {})
            return self._parse_introspection(raw, endpoint_url)
        except Exception as e:
            logger.error(f"GraphQL introspection failed for {endpoint_url}: {e}")
            return APISchema(source_url=endpoint_url, schema_type="graphql")

    def _parse_introspection(self, raw: dict, source_url: str) -> APISchema:
        schema = raw.get("__schema", {})
        types = schema.get("types", [])

        query_type_name = ""
        qt = schema.get("queryType")
        if qt:
            query_type_name = qt.get("name", "Query")

        mutation_type_name = ""
        mt = schema.get("mutationType")
        if mt:
            mutation_type_name = mt.get("name", "Mutation")

        endpoints: list[APIEndpoint] = []
        definitions: dict = {}

        for t in types:
            if t["name"].startswith("__"):
                continue
            kind = t.get("kind", "")
            fields = t.get("fields") or []
            type_name = t["name"]

            # Object/Interface type → definition에 저장
            if kind in ("OBJECT", "INTERFACE"):
                definitions[type_name] = {
                    "kind": kind,
                    "fields": {f["name"]: self._resolve_type(f.get("type", {})) for f in fields},
                    "description": t.get("description", ""),
                }

            # Query/Mutation fields → 엔드포인트로 변환
            if type_name in (query_type_name, mutation_type_name):
                for f in fields:
                    method = "QUERY" if type_name == query_type_name else "MUTATION"
                    endpoints.append(APIEndpoint(
                        path=f"/graphql/{type_name}/{f['name']}",
                        method=method,
                        summary=f.get("description", ""),
                        parameters=[
                            {
                                "name": a["name"],
                                "type": self._resolve_type(a.get("type", {})),
                            }
                            for a in (f.get("args") or [])
                        ],
                        response_schema={"type": self._resolve_type(f.get("type", {}))},
                        tags=[type_name],
                    ))

        return APISchema(
            source_url=source_url,
            schema_type="graphql",
            title=query_type_name,
            endpoints=endpoints,
            definitions=definitions,
            raw_schema=raw,
        )

    @staticmethod
    def _resolve_type(type_obj: dict) -> str:
        if not type_obj:
            return "Unknown"
        name = type_obj.get("name", "")
        kind = type_obj.get("kind", "")
        of_type = type_obj.get("ofType")
        if name:
            return name
        if of_type:
            inner = GraphQLCollector._resolve_type(of_type)
            if kind == "LIST":
                return f"[{inner}]"
            if kind == "NON_NULL":
                return f"{inner}!"
            return inner
        return kind

    def endpoints_to_table_schema(self, api_schema: APISchema) -> list[dict]:
        """GraphQL 타입을 DB 테이블 스키마처럼 변환"""
        tables = []
        for type_name, type_def in api_schema.definitions.items():
            if type_name.startswith("__"):
                continue
            fields = type_def.get("fields", {})
            if not fields:
                continue
            columns = []
            for field_name, field_type in fields.items():
                columns.append({
                    "column_name": field_name,
                    "data_type": field_type,
                    "is_nullable": True,
                    "default_value": None,
                    "table_name": type_name,
                    "schema_name": "graphql",
                    "source": "graphql",
                })
            tables.append({
                "table_name": type_name,
                "schema_name": "graphql",
                "columns": columns,
                "source": "graphql",
            })
        return tables


class RESTAPICollector:
    """일반 REST API에서 실제 요청으로 응답 스키마 추론"""

    def __init__(self, timeout: int = 30, max_sample_size: int = 100):
        self.timeout = timeout
        self.max_sample_size = max_sample_size

    def infer_from_response(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[dict] = None,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> dict:
        """실제 API 호출 후 응답에서 스키마 추론"""
        try:
            resp = httpx.request(
                method=method.upper(),
                url=url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return self._infer_schema(data)
        except Exception as e:
            logger.error(f"REST API inference failed for {url}: {e}")
            return {}

    def _infer_schema(self, data: Any, path: str = "") -> dict:
        if isinstance(data, dict):
            properties = {}
            for k, v in data.items():
                child = self._infer_schema(v, f"{path}.{k}" if path else k)
                properties[k] = child
            return {"type": "object", "properties": properties}
        elif isinstance(data, list):
            items_schema = {}
            sample = data[:self.max_sample_size]
            for item in sample:
                if isinstance(item, dict):
                    for k, v in item.items():
                        child_type = self._python_type(v)
                        if k not in items_schema:
                            items_schema[k] = {"type": child_type, "count": 1}
                        else:
                            items_schema[k]["count"] += 1
            total = len(sample)
            columns = []
            for k, info in items_schema.items():
                columns.append({
                    "column_name": k,
                    "data_type": info["type"],
                    "coverage": round(info["count"] / max(total, 1), 2),
                })
            return {"type": "array", "items": columns}
        else:
            return {"type": self._python_type(data)}

    @staticmethod
    def _python_type(val: Any) -> str:
        if isinstance(val, bool):
            return "boolean"
        if isinstance(val, int):
            return "integer"
        if isinstance(val, float):
            return "number"
        if isinstance(val, str):
            return "string"
        return "unknown"
