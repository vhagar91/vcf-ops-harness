"""
vROps REST API client for compliance reporting.
Handles authentication, resource management, and property pushing.
"""

import requests
import json
import logging
import time
from typing import Dict, List, Optional, Any, Tuple
from urllib.parse import urljoin
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

class VropsClient:
    """vROps REST API client for compliance reporting."""
    
    def __init__(self, server: str, username: str, password: str, auth_source: str = "Local"):
        """
        Initialize vROps client.
        
        Args:
            server: vROps server FQDN or IP
            username: vROps username
            password: vROps password
            auth_source: Authentication source (default: Local)
        """
        self.server = server
        self.username = username
        self.password = password
        self.auth_source = auth_source
        self.base_url = f"https://{server}/suite-api/api"
        self.token = None
        self.session = self._create_session()
        # Disable SSL warnings for self-signed certificates
        requests.packages.urllib3.disable_warnings()
        
    def _create_session(self) -> requests.Session:
        """Create a requests session with retry strategy."""
        session = requests.Session()
        
        # Configure retry strategy
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        return session
    
    def authenticate(self) -> bool:
        """
        Authenticate to vROps and get token.
        
        Returns:
            bool: True if authentication successful, False otherwise
        """
        try:
            url = f"https://{self.server}/suite-api/api/auth/token/acquire"
            headers = {
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            }
            
            auth_data = {
                'username': self.username,
                'password': self.password,
                'authSource': self.auth_source
            }
            
            response = self.session.post(
                url, 
                headers=headers, 
                json=auth_data,
                verify=False,
                timeout=30
            )
            
            if response.status_code == 200:
                self.token = response.json().get('token')
                logging.info("Successfully authenticated to vROps")
                return True
            else:
                logging.error(f"Authentication failed: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logging.error(f"Authentication error: {e}")
            return False
    
    def get_version(self) -> Optional[str]:
        """
        Get vROps version.
        
        Returns:
            str: vROps version or None if failed
        """
        try:
            url = f"https://{self.server}/suite-api/api/versions/current"
            headers = {
                'Authorization': f'vRealizeOpsToken {self.token}',
                'Accept': 'application/json'
            }
            
            response = self.session.get(url, headers=headers, verify=False, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                return data.get('releaseName')
            else:
                logging.error(f"Failed to get version: {response.status_code}")
                return None
                
        except Exception as e:
            logging.error(f"Error getting version: {e}")
            return None
    
    def find_resource(self, name: str, resource_kind: str, adapter_kind: str = "VMWARE") -> Optional[str]:
        """
        Find a resource by name and kind.
        
        Args:
            name: Resource name
            resource_kind: Resource kind (e.g., "HostSystem", "VirtualMachine")
            adapter_kind: Adapter kind (default: VMWARE)
            
        Returns:
            str: Resource ID if found, None otherwise
        """
        try:
            url = f"{self.base_url}/resources"
            params = {
                'adapterKind': adapter_kind,
                'resourceKind': resource_kind,
                'name': name
            }
            headers = {
                'Authorization': f'vRealizeOpsToken {self.token}',
                'Accept': 'application/json'
            }
            
            response = self.session.get(url, headers=headers, params=params, verify=False, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('resourceList'):
                    return data['resourceList'][0]['identifier']
            else:
                logging.debug(f"Resource not found: {name} ({resource_kind})")
                
            return None
            
        except Exception as e:
            logging.error(f"Error finding resource {name}: {e}")
            return None
    
    def get_resource_properties(self, resource_id: str) -> Optional[Dict]:
        """
        Get properties for a specific resource.
        
        Args:
            resource_id: Resource identifier
            
        Returns:
            Dict: Resource properties or None if failed
        """
        try:
            url = f"{self.base_url}/resources/{resource_id}/properties"
            headers = {
                'Authorization': f'vRealizeOpsToken {self.token}',
                'Accept': 'application/json'
            }
            
            response = self.session.get(url, headers=headers, verify=False, timeout=30)
            
            if response.status_code == 200:
                return response.json()
            else:
                logging.error(f"Failed to get properties: {response.status_code}")
                return None
                
        except Exception as e:
            logging.error(f"Error getting resource properties: {e}")
            return None
    
    def create_resource(self, adapter_kind: str, resource_kind: str, name: str, 
                       identifiers: Dict[str, str], description: str = "") -> Optional[str]:
        """
        Create a new resource in vROps.
        
        Args:
            adapter_kind: Adapter kind
            resource_kind: Resource kind
            name: Resource display name
            identifiers: Dictionary of resource identifiers
            description: Resource description
            
        Returns:
            str: Resource ID if created successfully, None otherwise
        """
        try:
            url = f"{self.base_url}/resources/adapterkinds/{adapter_kind}"
            
            # Build resource identifiers
            resource_identifiers = []
            for key, value in identifiers.items():
                logging.info(f"Adding identifier {key}: {value}")
                resource_identifiers.append({
                    'identifierType': {
                        'name': key,
                        'dataType': 'STRING',
                        'isPartOfUniqueness': True
                    },
                    'value': value
                })
            
            payload = {
                'description': description or f"{resource_kind} - {name}",
                'resourceKey': {
                    'name': name,
                    'adapterKindKey': adapter_kind,
                    'resourceKindKey': resource_kind,
                    'resourceIdentifiers': resource_identifiers
                },
                'dtEnabled': True,
                'monitoringInterval': 1440
            }
            
            headers = {
                'Authorization': f'vRealizeOpsToken {self.token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            }
            
            response = self.session.post(
                url, 
                headers=headers, 
                json=payload,
                verify=False,
                timeout=30
            )
            
            if response.status_code in [200, 201]:
                data = response.json()
                logging.info(f"Created resource {name} with ID: {data.get('identifier')}")
                return data.get('identifier')
            else:
                logging.error(f"Failed to create resource: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logging.error(f"Error creating resource: {e}")
            return None
    
    def push_properties(self, resource_id: str, properties: Dict[str, Any]) -> bool:
        """
        Push properties to a vROps resource.
        
        Args:
            resource_id: Resource identifier
            properties: Dictionary of properties to push
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            url = f"{self.base_url}/resources/properties"
            
            # Get current timestamp
            current_timestamp = int(time.time() * 1000)
            
            # Build property content
            property_content = []
            for key, value in properties.items():
                stat = {
                    'statKey': key,
                    'timestamps': [current_timestamp]
                }
                
                # Determine if value is numeric
                if isinstance(value, (int, float, bool)):
                    stat['data'] = [float(value)]
                else:
                    stat['values'] = [str(value)]
                
                property_content.append(stat)
            
            payload = {
                'values': [{
                    'resourceId': resource_id,
                    'property-contents': {
                        'property-content': property_content
                    }
                }]
            }
            
            headers = {
                'Authorization': f'vRealizeOpsToken {self.token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            }
            
            response = self.session.post(
                url, 
                headers=headers, 
                json=payload,
                verify=False,
                timeout=30
            )
            
            if response.status_code in [200, 201, 204]:
                logging.debug(f"Successfully pushed properties to resource {resource_id}")
                return True
            else:
                logging.error(f"Failed to push properties: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logging.error(f"Error pushing properties: {e}")
            return False
    
    def push_properties_batch(self, batch: List[Dict[str, Any]]) -> bool:
        """
        Push properties to multiple resources in a batch.
        
        Args:
            batch: List of dictionaries with 'ResId' and 'Props' keys
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            url = f"{self.base_url}/resources/properties"
            
            # Get current timestamp
            current_timestamp = int(time.time() * 1000)
            
            # Build batch payload
            values = []
            for item in batch:
                resource_id = item['ResId']
                properties = item['Props']
                
                property_content = []
                for key, value in properties.items():
                    stat = {
                        'statKey': key,
                        'timestamps': [current_timestamp]
                    }
                    
                    # Determine if value is numeric
                    if isinstance(value, (int, float, bool)):
                        stat['data'] = [float(value)]
                    else:
                        stat['values'] = [str(value)]
                    
                    property_content.append(stat)
                
                values.append({
                    'resourceId': resource_id,
                    'property-contents': {
                        'property-content': property_content
                    }
                })
            
            payload = {'values': values}
            
            headers = {
                'Authorization': f'vRealizeOpsToken {self.token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            }
            
            response = self.session.post(
                url, 
                headers=headers, 
                json=payload,
                verify=False,
                timeout=30
            )
            
            if response.status_code in [200, 201, 204]:
                logging.debug(f"Successfully pushed batch properties to {len(batch)} resources")
                return True
            else:
                logging.error(f"Failed to push batch properties: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logging.error(f"Error pushing batch properties: {e}")
            return False
    
    def push_event(self, resource_id: str, event_type: str, message: str, 
                  severity: str = "WARNING", start_time: Optional[int] = None) -> bool:
        """
        Push an event to a vROps resource.
        
        Args:
            resource_id: Resource identifier
            event_type: Event type
            message: Event message
            severity: Event severity (default: WARNING)
            start_time: Start time in milliseconds (default: now)
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            url = f"{self.base_url}/events"
            
            if start_time is None:
                start_time = int(time.time() * 1000)
            
            payload = {
                'eventType': event_type,
                'resourceId': resource_id,
                'cancelTimeUTC': 0,
                'startTimeUTC': start_time,
                'message': message,
                'severity': severity,
                'managedExternally': False
            }
            
            headers = {
                'Authorization': f'vRealizeOpsToken {self.token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            }
            
            response = self.session.post(
                url, 
                headers=headers, 
                json=payload,
                verify=False,
                timeout=30
            )
            
            if response.status_code in [200, 201, 204]:
                logging.info(f"Successfully pushed event to resource {resource_id}")
                return True
            else:
                logging.error(f"Failed to push event: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logging.error(f"Error pushing event: {e}")
            return False
    
    def add_child_relationship(self, parent_id: str, child_id: str) -> bool:
        """
        Add a child relationship between two resources.
        
        Args:
            parent_id: Parent resource ID
            child_id: Child resource ID
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            url = f"{self.base_url}/resources/{parent_id}/relationships/children?_no_links=true"
            
            payload = {'uuids': [child_id]}
            
            headers = {
                'Authorization': f'vRealizeOpsToken {self.token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            }
            
            response = self.session.post(
                url, 
                headers=headers, 
                json=payload,
                verify=False,
                timeout=30
            )
            
            if response.status_code in [200, 201, 204]:
                logging.debug(f"Successfully added child relationship: {parent_id} -> {child_id}")
                return True
            else:
                logging.error(f"Failed to add child relationship: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logging.error(f"Error adding child relationship: {e}")
            return False
    
    def find_resource_by_identifiers(self, adapter_kind: str, resource_kind: str, 
                                    identifiers: Dict[str, str]) -> Optional[str]:
        """
        Find a resource by its identifiers.
        
        Args:
            adapter_kind: Adapter kind
            resource_kind: Resource kind
            identifiers: Dictionary of identifier key-value pairs
            
        Returns:
            str: Resource ID if found, None otherwise
        """
        try:
            # First, get all resources of the specified kind
            url = f"{self.base_url}/resources"
            params = {
                'adapterKind': adapter_kind,
                'resourceKind': resource_kind
            }
            headers = {
                'Authorization': f'vRealizeOpsToken {self.token}',
                'Accept': 'application/json'
            }
            
            response = self.session.get(url, headers=headers, params=params, verify=False, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('resourceList'):
                    for resource in data['resourceList']:
                        # Check if this resource has the required identifiers
                        resource_identifiers = resource.get('resourceKey', {}).get('resourceIdentifiers', [])
                        
                        # Build a dict of the resource's identifiers
                        resource_id_dict = {}
                        for ri in resource_identifiers:
                            resource_id_dict[ri['identifierType']['name']] = ri['value']
                        
                        # Check if all required identifiers match
                        match = True
                        for key, value in identifiers.items():
                            if resource_id_dict.get(key) != value:
                                match = False
                                break
                        
                        if match:
                            return resource['identifier']
            
            return None
            
        except Exception as e:
            logging.error(f"Error finding resource by identifiers: {e}")
            return None
    
    def register_adapter_instance(self, adapter_instance_id: str = "ARCH_COMPLIANCE") -> bool:
        """
        Register the compliance adapter instance in vROps.
        Creates or verifies the existence of the adapter instance and its resource kinds.
        
        Args:
            adapter_instance_id: Adapter instance ID (default: ARCH_COMPLIANCE)
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Headers for internal API
            internal_headers = {
                'Authorization': f'vRealizeOpsToken {self.token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'X-Ops-API-use-unsupported': 'true'
            }
            
            # 1. Check if the Adapter Kind already exists
            url_check = f"https://{self.server}/suite-api/api/adapterkinds/{adapter_instance_id}"
            headers = {
                'Authorization': f'vRealizeOpsToken {self.token}',
                'Accept': 'application/json'
            }
            
            try:
                response = self.session.get(url_check, headers=headers, verify=False, timeout=30)
                if response.status_code == 200:
                    logging.info(f"Adapter kind '{adapter_instance_id}' already exists.")
                else:
                    logging.warning(f"Adapter kind '{adapter_instance_id}' not found, will be created automatically.")
            except Exception as e:
                logging.warning(f"Failed to check adapter kind '{adapter_instance_id}': {e}")
            
            # 2. Create Resource Kinds within the Adapter Kind
            resource_kinds_to_create = ["Certificate", "License", "StoragePolicy"]
            
            for resource_kind in resource_kinds_to_create:
                # Check if the resource kind already exists
                url_check_rk = f"https://{self.server}/suite-api/api/adapterkinds/{adapter_instance_id}/resourcekinds/{resource_kind}"
                
                try:
                    response = self.session.get(url_check_rk, headers=headers, verify=False, timeout=30)
                    if response.status_code == 200:
                        logging.info(f"Resource kind '{resource_kind}' already exists in adapter '{adapter_instance_id}'.")
                        continue
                except Exception as e:
                    logging.debug(f"Resource kind '{resource_kind}' not found: {e}")
                
                # Create the resource kind
                logging.info(f"Creating resource kind '{resource_kind}' in adapter '{adapter_instance_id}'...")
                url_create_rk = f"https://{self.server}/suite-api/internal/adapterkinds/{adapter_instance_id}/resourcekinds?_no_links=true"
                
                # Define resource identifier types based on resource kind
                resource_identifier_types = []
                if resource_kind == "License":
                    resource_identifier_types = [
                        {
                            'name': 'Serial',
                            'dataType': 'STRING',
                            'isPartOfUniqueness': True
                        }
                    ]
                elif resource_kind == "StoragePolicy":
                    resource_identifier_types = [
                        {
                            'name': 'PolicyId',
                            'dataType': 'STRING',
                            'isPartOfUniqueness': True
                        },
                        {
                            'name': 'vCenter',
                            'dataType': 'STRING',
                            'isPartOfUniqueness': True
                        }
                    ]
                
                payload = {
                    'key': resource_kind,
                    'resourceKindType': 'GENERAL',
                    'resourceKindSubType': 'NONE',
                    'resourceIdentifierTypes': resource_identifier_types
                }
                logging.info(f"Payload for creating resource kind '{resource_kind}': {payload}")
                
                try:
                    response = self.session.post(
                        url_create_rk,
                        headers=internal_headers,
                        json=payload,
                        verify=False,
                        timeout=30
                    )
                    
                    if response.status_code in [200, 201, 204]:
                        logging.info(f"Successfully created resource kind '{resource_kind}'.")
                    else:
                        logging.error(f"Failed to create resource kind '{resource_kind}': {response.status_code} - {response.text}")
                        return False
                        
                except Exception as e:
                    logging.error(f"Failed to create resource kind '{resource_kind}': {e}")
                    return False
            
            logging.info(f"Successfully registered adapter instance '{adapter_instance_id}' with all resource kinds.")
            return True
            
        except Exception as e:
            logging.error(f"Error registering adapter instance: {e}")
            return False
    
    def get_monitored_vcenters(self) -> List[Dict[str, str]]:
        """
        Discover monitored vCenters from vROps.
        
        Returns:
            List[Dict[str, str]]: List of vCenter dictionaries with 'name' and 'fqdn' keys
        """
        try:
            url = f"{self.base_url}/resources"
            params = {
                'adapterKind': 'VMWARE',
                'resourceKind': 'VMwareAdapter Instance'
            }
            headers = {
                'Authorization': f'vRealizeOpsToken {self.token}',
                'Accept': 'application/json'
            }
            
            response = self.session.get(url, headers=headers, params=params, verify=False, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                vcenters = []
                
                for resource in data.get('resourceList', []):
                    name = resource.get('resourceKey', {}).get('name', '')
                    identifier = resource.get('identifier', '')
                    
                    # Find the FQDN from resource identifiers
                    fqdn = name  # Default to name if FQDN not found
                    resource_identifiers = resource.get('resourceKey', {}).get('resourceIdentifiers', [])
                    
                    for ri in resource_identifiers:
                        if ri.get('identifierType', {}).get('name') == 'VCURL':
                            fqdn = ri.get('value', name)
                            break
                    
                    vcenters.append({
                        'name': name,
                        'fqdn': fqdn,
                        'identifier': identifier
                    })
                
                logging.info(f"Discovered {len(vcenters)} vCenter(s) from vROps")
                return vcenters
            else:
                logging.error(f"Failed to get monitored vCenters: {response.status_code}")
                return []
                
        except Exception as e:
            logging.error(f"Error discovering vCenters: {e}")
            return []
    
    def get_monitored_nsxt_managers(self) -> List[Dict[str, str]]:
        """
        Discover monitored NSX-T managers from vROps.
        
        Returns:
            List[Dict[str, str]]: List of NSX-T manager dictionaries with 'name' and 'fqdn' keys
        """
        try:
            url = f"{self.base_url}/resources"
            params = {
                'adapterKind': 'NSXTAdapter',
                'resourceKind': 'NSXTAdapterInstance'
            }
            headers = {
                'Authorization': f'vRealizeOpsToken {self.token}',
                'Accept': 'application/json'
            }
            
            response = self.session.get(url, headers=headers, params=params, verify=False, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                managers = []
                
                for resource in data.get('resourceList', []):
                    name = resource.get('resourceKey', {}).get('name', '')
                    identifier = resource.get('identifier', '')
                    
                    # Find the FQDN from resource identifiers
                    fqdn = name  # Default to name if FQDN not found
                    resource_identifiers = resource.get('resourceKey', {}).get('resourceIdentifiers', [])
                    
                    for ri in resource_identifiers:
                        if ri.get('identifierType', {}).get('name') == 'NSXTHOST':
                            fqdn = ri.get('value', name)
                            break
                    
                    managers.append({
                        'name': name,
                        'fqdn': fqdn,
                        'identifier': identifier
                    })
                
                logging.info(f"Discovered {len(managers)} NSX-T manager(s) from vROps")
                return managers
            else:
                logging.error(f"Failed to get monitored NSX-T managers: {response.status_code}")
                return []

        except Exception as e:
            logging.error(f"Error discovering NSX-T managers: {e}")
            return []

    # ----------------------------------------------------------------------
    # Read API (alerts / health / performance) — used by the LLM tools.
    # All routed through _request, which re-authenticates once on a 401 so a
    # long-running bot self-heals when the token expires.
    # ----------------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Send an authenticated request; re-auth once on 401 and retry."""
        url = f"{self.base_url}{path}"
        headers = kwargs.pop("headers", {}) or {}
        headers.setdefault("Accept", "application/json")
        headers["Authorization"] = f"vRealizeOpsToken {self.token}"
        kwargs.setdefault("verify", False)
        kwargs.setdefault("timeout", 30)

        resp = self.session.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 401:
            logging.info("vROps token expired/invalid; re-authenticating")
            if self.authenticate():
                headers["Authorization"] = f"vRealizeOpsToken {self.token}"
                resp = self.session.request(method, url, headers=headers, **kwargs)
        return resp

    def search_resources(self, name: str, resource_kind: Optional[str] = None,
                         adapter_kind: Optional[str] = None,
                         page_size: int = 50) -> List[Dict[str, Any]]:
        """Find resources by (partial) name. Returns all matches, not just the first."""
        params: Dict[str, Any] = {"name": name, "pageSize": page_size}
        if resource_kind:
            params["resourceKind"] = resource_kind
        if adapter_kind:
            params["adapterKind"] = adapter_kind
        try:
            resp = self._request("GET", "/resources", params=params)
            if resp.status_code != 200:
                logging.error(f"search_resources failed: {resp.status_code}")
                return []
            out = []
            for r in resp.json().get("resourceList", []):
                rk = r.get("resourceKey", {})
                out.append({
                    "identifier": r.get("identifier"),
                    "name": rk.get("name"),
                    "resourceKind": rk.get("resourceKindKey"),
                    "adapterKind": rk.get("adapterKindKey"),
                    "health": r.get("resourceHealth"),
                    "healthValue": r.get("resourceHealthValue"),
                })
            return out
        except Exception as e:
            logging.error(f"Error searching resources '{name}': {e}")
            return []

    def get_resource_health(self, resource_id: str) -> Optional[Dict[str, Any]]:
        """Get health/state for a resource."""
        try:
            resp = self._request("GET", f"/resources/{resource_id}")
            if resp.status_code != 200:
                logging.error(f"get_resource_health failed: {resp.status_code}")
                return None
            r = resp.json()
            rk = r.get("resourceKey", {})
            states = [
                {
                    "adapterInstanceId": s.get("adapterInstanceId"),
                    "resourceStatus": s.get("resourceStatus"),
                    "resourceState": s.get("resourceState"),
                    "statusMessage": s.get("statusMessage"),
                }
                for s in r.get("resourceStatusStates", [])
            ]
            return {
                "identifier": r.get("identifier"),
                "name": rk.get("name"),
                "resourceKind": rk.get("resourceKindKey"),
                "health": r.get("resourceHealth"),
                "healthValue": r.get("resourceHealthValue"),
                "states": states,
            }
        except Exception as e:
            logging.error(f"Error getting health for {resource_id}: {e}")
            return None

    def get_alerts(self, resource_id: Optional[str] = None,
                   criticality: Optional[str] = None,
                   active_only: bool = True,
                   page_size: int = 100) -> List[Dict[str, Any]]:
        """List alerts, optionally filtered by resource and/or criticality."""
        params: Dict[str, Any] = {
            "page": 0,
            "pageSize": page_size,
            "activeOnly": str(active_only).lower(),
        }
        if resource_id:
            params["resourceId"] = resource_id
        if criticality:
            params["alertCriticality"] = criticality
        try:
            resp = self._request("GET", "/alerts", params=params)
            if resp.status_code != 200:
                logging.error(f"get_alerts failed: {resp.status_code}")
                return []
            out = []
            for a in resp.json().get("alerts", []):
                out.append({
                    "alertId": a.get("alertId"),
                    "name": a.get("alertDefinitionName"),
                    "level": a.get("alertLevel"),
                    "status": a.get("status"),
                    "resourceId": a.get("resourceId"),
                    "startTimeUTC": a.get("startTimeUTC"),
                    "impact": a.get("alertImpact") or a.get("impact"),
                })
            # When only active alerts are requested, exclude canceled ones
            # since the API's activeOnly filter may still return them.
            if active_only:
                out = [a for a in out if a["status"] != "CANCELED"]
            return out
        except Exception as e:
            logging.error(f"Error getting alerts: {e}")
            return []

    def get_alert(self, alert_id: str) -> Optional[Dict[str, Any]]:
        """Get full detail for a single alert."""
        try:
            resp = self._request("GET", f"/alerts/{alert_id}")
            if resp.status_code != 200:
                logging.error(f"get_alert failed: {resp.status_code}")
                return None
            return resp.json()
        except Exception as e:
            logging.error(f"Error getting alert {alert_id}: {e}")
            return None

    def get_stat_keys(self, resource_id: str) -> List[str]:
        """Discover the metric (stat) keys available for a resource."""
        try:
            resp = self._request("GET", f"/resources/{resource_id}/statkeys")
            if resp.status_code != 200:
                logging.error(f"get_stat_keys failed: {resp.status_code}")
                return []
            data = resp.json()
            keys = data.get("stat-key") or data.get("statKeys") or []
            return [k.get("key") for k in keys if isinstance(k, dict) and k.get("key")]
        except Exception as e:
            logging.error(f"Error getting stat keys for {resource_id}: {e}")
            return []

    def get_latest_stats(self, resource_id: str,
                         stat_keys: Optional[List[str]] = None) -> Dict[str, Any]:
        """Get the most recent value for each requested metric (or all)."""
        params: Dict[str, Any] = {}
        if stat_keys:
            params["statKey"] = stat_keys  # requests encodes list as repeated params
        try:
            resp = self._request("GET", f"/resources/{resource_id}/stats/latest", params=params)
            if resp.status_code != 200:
                logging.error(f"get_latest_stats failed: {resp.status_code}")
                return {}
            out: Dict[str, Any] = {}
            for v in resp.json().get("values", []):
                for stat in v.get("stat-list", {}).get("stat", []):
                    key = stat.get("statKey", {}).get("key")
                    data = stat.get("data") or []
                    if key and data:
                        out[key] = data[-1]
            return out
        except Exception as e:
            logging.error(f"Error getting latest stats for {resource_id}: {e}")
            return {}

    def get_stats(self, resource_id: str, stat_keys: List[str],
                  hours_back: float = 6.0, rollup: str = "AVG",
                  interval: str = "MINUTES", interval_qty: int = 5) -> Dict[str, Any]:
        """Get a time-series for metrics, returned as a compact summary
        (count/latest/min/max/avg) rather than every raw data point."""
        end = int(time.time() * 1000)
        begin = end - int(hours_back * 3600 * 1000)
        params: Dict[str, Any] = {
            "statKey": stat_keys,
            "begin": begin,
            "end": end,
            "rollUpType": rollup,
            "intervalType": interval,
            "intervalQuantifier": interval_qty,
        }
        try:
            resp = self._request("GET", f"/resources/{resource_id}/stats", params=params)
            if resp.status_code != 200:
                logging.error(f"get_stats failed: {resp.status_code}")
                return {}
            summary: Dict[str, Any] = {}
            for v in resp.json().get("values", []):
                for stat in v.get("stat-list", {}).get("stat", []):
                    key = stat.get("statKey", {}).get("key")
                    data = [d for d in (stat.get("data") or []) if d is not None]
                    if key and data:
                        summary[key] = {
                            "count": len(data),
                            "latest": data[-1],
                            "min": min(data),
                            "max": max(data),
                            "avg": round(sum(data) / len(data), 3),
                        }
            return summary
        except Exception as e:
            logging.error(f"Error getting stats for {resource_id}: {e}")
            return {}

    def get_stat_series(self, resource_id: str, stat_keys: List[str],
                        hours_back: float = 24.0, rollup: str = "AVG",
                        interval: str = "MINUTES", interval_qty: int = 5) -> Dict[str, List[float]]:
        """Like get_stats, but returns the ordered (non-null) data points per key,
        so callers can compute trends and threshold-breach counts."""
        end = int(time.time() * 1000)
        begin = end - int(hours_back * 3600 * 1000)
        params: Dict[str, Any] = {
            "statKey": stat_keys,
            "begin": begin,
            "end": end,
            "rollUpType": rollup,
            "intervalType": interval,
            "intervalQuantifier": interval_qty,
        }
        try:
            resp = self._request("GET", f"/resources/{resource_id}/stats", params=params)
            if resp.status_code != 200:
                logging.error(f"get_stat_series failed: {resp.status_code}")
                return {}
            series: Dict[str, List[float]] = {}
            for v in resp.json().get("values", []):
                for stat in v.get("stat-list", {}).get("stat", []):
                    key = stat.get("statKey", {}).get("key")
                    data = [d for d in (stat.get("data") or []) if d is not None]
                    if key:
                        series[key] = data
            return series
        except Exception as e:
            logging.error(f"Error getting stat series for {resource_id}: {e}")
            return {}
