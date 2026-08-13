from collections.abc import Callable
from typing import Any, Optional, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.strategy_platform_errors import StrategyPlatformReadError
from app.db.session import get_db
from app.schemas.strategy_platform import (
    ActiveConfigurationRead,
    ConfigurationAuditEventListRead,
    ConfigurationBundleResolutionRead,
    ConfigurationBundleResolveRequest,
    ConfigurationBundleSnapshotListRead,
    ConfigurationBundleSnapshotRead,
    ConfigurationCatalogRead,
    ConfigurationDraftCreateRequest,
    ConfigurationVersionActionRequest,
    ConfigurationVersionDetailRead,
    ConfigurationVersionDiffRead,
    ConfigurationVersionListRead,
    ConfigurationWriteResult,
    StrategyCatalogPageRead,
    StrategyValidationHistoryRead,
)
from app.services.configuration_management import ConfigurationManagementService
from app.services.configuration_resolver import ConfigurationResolverService
from app.services.operator_authorization import (
    OperatorRequestHeaders,
    operator_request_coordinator,
    operator_request_headers,
)
from app.services.owner_read_access import OwnerReadAccess, require_owner_read_access
from app.services.strategy_platform_read import StrategyPlatformReadService

router = APIRouter(prefix="/api/v1", tags=["strategy-platform-v1.3"])
_WriteResult = TypeVar("_WriteResult")


def _execute_configuration_write(
    *,
    db: Session,
    headers: OperatorRequestHeaders,
    operation: str,
    request_payload: dict[str, Any],
    handler: Callable[[str], _WriteResult],
) -> _WriteResult:
    def execute() -> _WriteResult:
        try:
            result = handler(headers.idempotency_key or "")
            db.commit()
            return result
        except StrategyPlatformReadError as exc:
            db.rollback()
            failure_event = {
                "configuration.validate": "VALIDATION_FAILED",
                "configuration.activate": "ACTIVATION_FAILED",
            }.get(operation)
            version_id = request_payload.get("version_id")
            scope_type = request_payload.get("scope_type")
            scope_key = request_payload.get("scope_key")
            if (
                failure_event is not None
                and isinstance(version_id, int)
                and isinstance(scope_type, str)
                and isinstance(scope_key, str)
            ):
                try:
                    ConfigurationManagementService(db).record_failed_write(
                        operation=operation,
                        event_type=failure_event,
                        config_type=str(request_payload.get("config_type") or ""),
                        version_id=version_id,
                        scope_type=scope_type,
                        scope_key=scope_key,
                        request_id=headers.idempotency_key or "",
                        request_payload=request_payload,
                        error=exc,
                    )
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.detail(),
            ) from exc
        except Exception:
            db.rollback()
            raise

    return operator_request_coordinator.execute(
        headers,
        operation=operation,
        provider_call=False,
        request_payload=request_payload,
        handler=execute,
        cache_result=lambda _result: False,
    )


@router.get("/configuration-catalog", response_model=ConfigurationCatalogRead)
def configuration_catalog(
    db: Session = Depends(get_db),
    _owner: OwnerReadAccess = Depends(require_owner_read_access),
) -> ConfigurationCatalogRead:
    return ConfigurationResolverService(db).catalog()


@router.get(
    "/configurations/{config_type}/versions",
    response_model=ConfigurationVersionListRead,
)
def configuration_versions(
    config_type: str,
    scope_type: str = Query(min_length=1, max_length=80),
    scope_key: str = Query(min_length=1, max_length=160),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    _owner: OwnerReadAccess = Depends(require_owner_read_access),
) -> ConfigurationVersionListRead:
    return ConfigurationResolverService(db).list_versions(
        config_type=config_type,
        scope_type=scope_type,
        scope_key=scope_key,
        limit=limit,
    )


@router.get(
    "/configurations/{config_type}/versions/{version_id}",
    response_model=ConfigurationVersionDetailRead,
)
def configuration_version_detail(
    config_type: str,
    version_id: int,
    db: Session = Depends(get_db),
    _owner: OwnerReadAccess = Depends(require_owner_read_access),
) -> ConfigurationVersionDetailRead:
    return ConfigurationManagementService(db).version_detail(
        config_type=config_type,
        version_id=version_id,
    )


@router.post(
    "/configurations/{config_type}/versions",
    response_model=ConfigurationWriteResult,
    status_code=201,
)
def create_configuration_draft(
    config_type: str,
    payload: ConfigurationDraftCreateRequest,
    db: Session = Depends(get_db),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> ConfigurationWriteResult:
    request_payload = {"config_type": config_type, **payload.model_dump(mode="json")}
    return _execute_configuration_write(
        db=db,
        headers=operator_headers,
        operation="configuration.create_draft",
        request_payload=request_payload,
        handler=lambda request_id: ConfigurationManagementService(db).create_draft(
            config_type=config_type,
            request=payload,
            request_id=request_id,
        ),
    )


@router.post(
    "/configurations/{config_type}/versions/{version_id}/validate",
    response_model=ConfigurationWriteResult,
)
def validate_configuration_version(
    config_type: str,
    version_id: int,
    payload: ConfigurationVersionActionRequest,
    db: Session = Depends(get_db),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> ConfigurationWriteResult:
    request_payload = {
        "config_type": config_type,
        "version_id": version_id,
        **payload.model_dump(mode="json"),
    }
    return _execute_configuration_write(
        db=db,
        headers=operator_headers,
        operation="configuration.validate",
        request_payload=request_payload,
        handler=lambda request_id: ConfigurationManagementService(db).validate_version(
            config_type=config_type,
            version_id=version_id,
            request=payload,
            request_id=request_id,
        ),
    )


@router.post(
    "/configurations/{config_type}/versions/{version_id}/activate",
    response_model=ConfigurationWriteResult,
)
def activate_configuration_version(
    config_type: str,
    version_id: int,
    payload: ConfigurationVersionActionRequest,
    db: Session = Depends(get_db),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> ConfigurationWriteResult:
    request_payload = {
        "config_type": config_type,
        "version_id": version_id,
        **payload.model_dump(mode="json"),
    }
    return _execute_configuration_write(
        db=db,
        headers=operator_headers,
        operation="configuration.activate",
        request_payload=request_payload,
        handler=lambda request_id: ConfigurationManagementService(db).activate_version(
            config_type=config_type,
            version_id=version_id,
            request=payload,
            request_id=request_id,
        ),
    )


@router.post(
    "/configurations/{config_type}/versions/{version_id}/retire",
    response_model=ConfigurationWriteResult,
)
def retire_configuration_version(
    config_type: str,
    version_id: int,
    payload: ConfigurationVersionActionRequest,
    db: Session = Depends(get_db),
    operator_headers: OperatorRequestHeaders = Depends(operator_request_headers),
) -> ConfigurationWriteResult:
    request_payload = {
        "config_type": config_type,
        "version_id": version_id,
        **payload.model_dump(mode="json"),
    }
    return _execute_configuration_write(
        db=db,
        headers=operator_headers,
        operation="configuration.retire",
        request_payload=request_payload,
        handler=lambda request_id: ConfigurationManagementService(db).retire_version(
            config_type=config_type,
            version_id=version_id,
            request=payload,
            request_id=request_id,
        ),
    )


@router.get(
    "/configurations/{config_type}/versions/{version_id}/diff",
    response_model=ConfigurationVersionDiffRead,
)
def configuration_version_diff(
    config_type: str,
    version_id: int,
    scope_type: str = Query(min_length=1, max_length=80),
    scope_key: str = Query(min_length=1, max_length=160),
    against_version_id: Optional[int] = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    _owner: OwnerReadAccess = Depends(require_owner_read_access),
) -> ConfigurationVersionDiffRead:
    return ConfigurationManagementService(db).diff_versions(
        config_type=config_type,
        version_id=version_id,
        against_version_id=against_version_id,
        scope_type=scope_type,
        scope_key=scope_key,
    )


@router.get(
    "/configurations/{config_type}/audit-events",
    response_model=ConfigurationAuditEventListRead,
)
def configuration_audit_events(
    config_type: str,
    scope_type: str = Query(min_length=1, max_length=80),
    scope_key: str = Query(min_length=1, max_length=160),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _owner: OwnerReadAccess = Depends(require_owner_read_access),
) -> ConfigurationAuditEventListRead:
    return ConfigurationManagementService(db).audit_history(
        config_type=config_type,
        scope_type=scope_type,
        scope_key=scope_key,
        limit=limit,
    )


@router.get(
    "/configurations/{config_type}/active",
    response_model=ActiveConfigurationRead,
)
def active_configuration(
    config_type: str,
    scope_type: str = Query(min_length=1, max_length=80),
    scope_key: str = Query(min_length=1, max_length=160),
    db: Session = Depends(get_db),
    _owner: OwnerReadAccess = Depends(require_owner_read_access),
) -> ActiveConfigurationRead:
    return ConfigurationResolverService(db).active_configuration(
        config_type=config_type,
        scope_type=scope_type,
        scope_key=scope_key,
    )


@router.post(
    "/configuration-bundles/resolve",
    response_model=ConfigurationBundleResolutionRead,
)
def resolve_configuration_bundle(
    payload: ConfigurationBundleResolveRequest,
    db: Session = Depends(get_db),
    _owner: OwnerReadAccess = Depends(require_owner_read_access),
) -> ConfigurationBundleResolutionRead:
    """Preview the exact active graph without persisting a task or snapshot."""

    return ConfigurationResolverService(db).resolve_active(
        workflow_kind=payload.workflow_kind,
        aggregate_config_type=payload.aggregate_config_type,
        scope_type=payload.scope_type,
        scope_key=payload.scope_key,
    )


@router.get(
    "/configuration-bundles",
    response_model=ConfigurationBundleSnapshotListRead,
)
def configuration_bundle_history(
    scope_type: str = Query(min_length=1, max_length=80),
    scope_key: str = Query(min_length=1, max_length=160),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    _owner: OwnerReadAccess = Depends(require_owner_read_access),
) -> ConfigurationBundleSnapshotListRead:
    return ConfigurationManagementService(db).bundle_history(
        scope_type=scope_type,
        scope_key=scope_key,
        limit=limit,
    )


@router.get(
    "/configuration-bundles/{bundle_id}",
    response_model=ConfigurationBundleSnapshotRead,
)
def configuration_bundle(
    bundle_id: int,
    db: Session = Depends(get_db),
    _owner: OwnerReadAccess = Depends(require_owner_read_access),
) -> ConfigurationBundleSnapshotRead:
    return ConfigurationResolverService(db).read_bundle(bundle_id)


@router.get("/strategy-catalog", response_model=StrategyCatalogPageRead)
def strategy_catalog(
    limit: int = Query(default=50, ge=1, le=200),
    cursor: Optional[str] = Query(default=None, min_length=1, max_length=512),
    db: Session = Depends(get_db),
    _owner: OwnerReadAccess = Depends(require_owner_read_access),
) -> StrategyCatalogPageRead:
    return StrategyPlatformReadService(db).strategy_catalog(limit=limit, cursor=cursor)


@router.get(
    "/strategies/{strategy_id}/validation-history",
    response_model=StrategyValidationHistoryRead,
)
def strategy_validation_history(
    strategy_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    _owner: OwnerReadAccess = Depends(require_owner_read_access),
) -> StrategyValidationHistoryRead:
    return StrategyPlatformReadService(db).validation_history(
        strategy_id=strategy_id,
        limit=limit,
    )
