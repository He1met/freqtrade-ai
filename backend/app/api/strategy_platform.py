from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.strategy_platform import (
    ActiveConfigurationRead,
    ConfigurationBundleResolutionRead,
    ConfigurationBundleResolveRequest,
    ConfigurationBundleSnapshotRead,
    ConfigurationCatalogRead,
    ConfigurationVersionListRead,
    StrategyCatalogPageRead,
    StrategyValidationHistoryRead,
)
from app.services.configuration_resolver import ConfigurationResolverService
from app.services.owner_read_access import OwnerReadAccess, require_owner_read_access
from app.services.strategy_platform_read import StrategyPlatformReadService

router = APIRouter(prefix="/api/v1", tags=["strategy-platform-v1.3"])


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
