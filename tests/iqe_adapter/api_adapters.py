"""
API-Specific Adapters for IQE Compatibility.

These adapters provide the specific API interfaces that IQE tests use:
- cost_models_api: Cost model CRUD operations
- integrations_api: Source/integration management

Each adapter translates IQE method calls to REST API requests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from .exceptions import ApiException, NotFoundException


@dataclass
class CostModelResponse:
    """
    IQE-compatible cost model response object.
    
    Matches the structure returned by IQE's cost_models_api methods.
    """
    uuid: str
    name: str
    description: str = ""
    source_type: str = ""
    rates: List[Dict[str, Any]] = field(default_factory=list)
    sources: List[Dict[str, Any]] = field(default_factory=list)
    markup: Optional[Dict[str, Any]] = None
    currency: str = "USD"
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "uuid": self.uuid,
            "name": self.name,
            "description": self.description,
            "source_type": self.source_type,
            "rates": self.rates,
            "sources": self.sources,
            "markup": self.markup,
            "currency": self.currency,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CostModelResponse":
        """Create from API response dictionary."""
        return cls(
            uuid=data.get("uuid", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            source_type=data.get("source_type", ""),
            rates=data.get("rates", []),
            sources=data.get("sources", []),
            markup=data.get("markup"),
            currency=data.get("currency", "USD"),
        )


class CostModelsApiAdapter:
    """
    Provides cost_models_api interface.
    
    IQE uses:
        application.cost_management.rest_client.cost_models_api.list_cost_models()
        application.cost_management.rest_client.cost_models_api.get_cost_model(uuid)
        application.cost_management.rest_client.cost_models_api.create_cost_model(payload)
        application.cost_management.rest_client.cost_models_api.update_cost_model(uuid, payload)
        application.cost_management.rest_client.cost_models_api.delete_cost_model(uuid)
    """
    
    def __init__(self, base_url: str, session: requests.Session):
        self.base_url = f"{base_url.rstrip('/')}/cost-models"
        self.session = session
    
    def _handle_response(self, response: requests.Response) -> Dict[str, Any]:
        """Handle response and raise appropriate exceptions."""
        if response.status_code == 404:
            raise NotFoundException(reason="Cost model not found")
        if response.status_code >= 400:
            try:
                body = response.text
            except Exception:
                body = ""
            raise ApiException(
                status=response.status_code,
                reason=response.reason,
                body=body,
            )
        return response.json()
    
    def list_cost_models(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        name: Optional[str] = None,
        source_type: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        List cost models.
        
        Returns:
            Dict with 'meta', 'links', and 'data' keys
        """
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if name is not None:
            params["name"] = name
        if source_type is not None:
            params["source_type"] = source_type
        params.update(kwargs)
        
        response = self.session.get(f"{self.base_url}/", params=params or None)
        return self._handle_response(response)
    
    def get_cost_model(self, cost_model_uuid: str) -> CostModelResponse:
        """
        Get a specific cost model by UUID.
        
        Args:
            cost_model_uuid: The cost model UUID
            
        Returns:
            CostModelResponse object
        """
        response = self.session.get(f"{self.base_url}/{cost_model_uuid}/")
        data = self._handle_response(response)
        return CostModelResponse.from_dict(data)
    
    def create_cost_model(self, cost_model: Dict[str, Any]) -> CostModelResponse:
        """
        Create a new cost model.
        
        Args:
            cost_model: Cost model payload
            
        Returns:
            CostModelResponse object
        """
        response = self.session.post(
            f"{self.base_url}/",
            json=cost_model,
            allow_redirects=False,
        )
        data = self._handle_response(response)
        return CostModelResponse.from_dict(data)
    
    def update_cost_model(
        self,
        cost_model_uuid: str,
        cost_model: Dict[str, Any]
    ) -> CostModelResponse:
        """
        Update an existing cost model.
        
        Args:
            cost_model_uuid: The cost model UUID
            cost_model: Updated cost model payload
            
        Returns:
            CostModelResponse object
        """
        response = self.session.put(
            f"{self.base_url}/{cost_model_uuid}/",
            json=cost_model,
        )
        data = self._handle_response(response)
        return CostModelResponse.from_dict(data)
    
    def delete_cost_model(self, cost_model_uuid: str) -> None:
        """
        Delete a cost model.
        
        Args:
            cost_model_uuid: The cost model UUID
        """
        response = self.session.delete(f"{self.base_url}/{cost_model_uuid}/")
        if response.status_code == 404:
            raise NotFoundException(reason="Cost model not found")
        if response.status_code >= 400:
            raise ApiException(
                status=response.status_code,
                reason=response.reason,
            )


@dataclass
class SourceResponse:
    """
    IQE-compatible source response object.
    
    Matches the structure returned by IQE's integrations_api methods.
    """
    id: int
    uuid: str
    name: str
    source_type: str = ""
    authentication: Dict[str, Any] = field(default_factory=dict)
    billing_source: Optional[Dict[str, Any]] = None
    cost_models: List[Dict[str, Any]] = field(default_factory=list)
    infrastructure: Optional[Dict[str, Any]] = None
    
    # Additional fields that may be present
    created_timestamp: Optional[str] = None
    updated_timestamp: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "uuid": self.uuid,
            "name": self.name,
            "source_type": self.source_type,
            "authentication": self.authentication,
            "billing_source": self.billing_source,
            "cost_models": self.cost_models,
            "infrastructure": self.infrastructure,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SourceResponse":
        """Create from API response dictionary."""
        return cls(
            id=data.get("id", 0),
            uuid=data.get("uuid", ""),
            name=data.get("name", ""),
            source_type=data.get("source_type", ""),
            authentication=data.get("authentication", {}),
            billing_source=data.get("billing_source"),
            cost_models=data.get("cost_models", []),
            infrastructure=data.get("infrastructure"),
            created_timestamp=data.get("created_timestamp"),
            updated_timestamp=data.get("updated_timestamp"),
        )


class IntegrationsApiAdapter:
    """
    Provides integrations_api interface.
    
    IQE uses:
        application.cost_management.rest_client.integrations_api.get_source(source_id)
        application.cost_management.rest_client.integrations_api.list_sources()
        application.cost_management.rest_client.integrations_api.get_source_stats(source_id)
    """
    
    def __init__(self, base_url: str, session: requests.Session):
        self.base_url = f"{base_url.rstrip('/')}/sources"
        self.session = session
    
    def _handle_response(self, response: requests.Response) -> Dict[str, Any]:
        """Handle response and raise appropriate exceptions."""
        if response.status_code == 404:
            raise NotFoundException(reason="Source not found")
        if response.status_code >= 400:
            try:
                body = response.text
            except Exception:
                body = ""
            raise ApiException(
                status=response.status_code,
                reason=response.reason,
                body=body,
            )
        return response.json()
    
    def list_sources(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        name: Optional[str] = None,
        source_type: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        List sources.
        
        Returns:
            Dict with 'meta', 'links', and 'data' keys
        """
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if name is not None:
            params["name"] = name
        if source_type is not None:
            params["source_type"] = source_type
        params.update(kwargs)
        
        response = self.session.get(f"{self.base_url}/", params=params or None)
        return self._handle_response(response)
    
    def get_source(self, source_id: int) -> SourceResponse:
        """
        Get a specific source by ID.
        
        Args:
            source_id: The source ID
            
        Returns:
            SourceResponse object
        """
        response = self.session.get(f"{self.base_url}/{source_id}/")
        data = self._handle_response(response)
        return SourceResponse.from_dict(data)
    
    def get_source_stats(self, source_id: int) -> Dict[str, Any]:
        """
        Get statistics for a source.
        
        Args:
            source_id: The source ID
            
        Returns:
            Dict with source statistics
        """
        response = self.session.get(f"{self.base_url}/{source_id}/stats/")
        return self._handle_response(response)
    
    def create_source(self, source: Dict[str, Any]) -> SourceResponse:
        """
        Create a new source.
        
        Args:
            source: Source payload
            
        Returns:
            SourceResponse object
        """
        response = self.session.post(
            f"{self.base_url}/",
            json=source,
            allow_redirects=False,
        )
        data = self._handle_response(response)
        return SourceResponse.from_dict(data)
    
    def delete_source(self, source_id: int) -> None:
        """
        Delete a source.
        
        Args:
            source_id: The source ID
        """
        response = self.session.delete(f"{self.base_url}/{source_id}/")
        if response.status_code == 404:
            raise NotFoundException(reason="Source not found")
        if response.status_code >= 400:
            raise ApiException(
                status=response.status_code,
                reason=response.reason,
            )


__all__ = [
    "CostModelResponse",
    "CostModelsApiAdapter",
    "SourceResponse",
    "IntegrationsApiAdapter",
]
