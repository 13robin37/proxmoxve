"""DataUpdateCoordinators for the Proxmox VE integration."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from proxmoxer import AuthenticationError, ProxmoxAPI
from proxmoxer.core import ResourceException
from requests.exceptions import ConnectTimeout, SSLError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.typing import UNDEFINED, UndefinedType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import get_api
from .const import CONF_NODE, DOMAIN, LOGGER, UPDATE_INTERVAL, ProxmoxType
from .models import (
    ProxmoxDiskData,
    ProxmoxLXCData,
    ProxmoxNodeData,
    ProxmoxStorageData,
    ProxmoxUpdateData,
    ProxmoxVMData,
)


class ProxmoxCoordinator(
    DataUpdateCoordinator[ProxmoxNodeData | ProxmoxVMData | ProxmoxLXCData]
):
    """Proxmox VE data update coordinator."""


class ProxmoxNodeCoordinator(ProxmoxCoordinator):
    """Proxmox VE Node data update coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        proxmox: ProxmoxAPI,
        api_category: str,
        node_name: str,
    ) -> None:
        """Initialize the Proxmox Node coordinator."""

        super().__init__(
            hass,
            LOGGER,
            name=f"proxmox_coordinator_{api_category}_{node_name}",
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

        self.hass = hass
        self.config_entry: ConfigEntry = self.config_entry
        self.proxmox = proxmox
        self.resource_id = node_name
        self.api_category = api_category

    async def _async_update_data(self) -> ProxmoxNodeData:
        """Update data  for Proxmox Node."""

        api_path = f"nodes/{self.resource_id}/status"
        api_status = await self.hass.async_add_executor_job(
            poll_api,
            self.hass,
            self.config_entry,
            self.proxmox,
            api_path,
            ProxmoxType.Node,
            self.resource_id,
        )
        if api_status is None:
            raise UpdateFailed(
                f"Node {self.resource_id} unable to be found in host {self.config_entry.data[CONF_HOST]}"
            )

        api_path = "nodes"
        if nodes_api := await self.hass.async_add_executor_job(
            poll_api,
            self.hass,
            self.config_entry,
            self.proxmox,
            api_path,
            ProxmoxType.Node,
            self.resource_id,
        ):
            for node_api in nodes_api:
                if node_api[CONF_NODE] == self.resource_id:
                    api_status["status"] = node_api["status"]
                    api_status["cpu"] = node_api["cpu"]
                    api_status["disk_max"] = node_api["maxdisk"]
                    api_status["disk_used"] = node_api["disk"]
                    break

        api_path = f"nodes/{self.resource_id}/version"
        api_status["version"] = await self.hass.async_add_executor_job(
            poll_api,
            self.hass,
            self.config_entry,
            self.proxmox,
            api_path,
            ProxmoxType.Node,
            self.resource_id,
        )

        api_path = f"nodes/{self.resource_id}/qemu"
        qemu_status = await self.hass.async_add_executor_job(
            poll_api,
            self.hass,
            self.config_entry,
            self.proxmox,
            api_path,
            ProxmoxType.QEMU,
            self.resource_id,
        )
        node_qemu = {}
        node_qemu["total"] = 0
        node_qemu["list"] = []
        for qemu in qemu_status:
            if qemu["status"] == "running":
                node_qemu["total"] += 1
                node_qemu["list"].append(f"{qemu['name']} ({qemu['vmid']})")
        api_status["qemu"] = node_qemu

        api_path = f"nodes/{self.resource_id}/lxc"
        lxc_status = await self.hass.async_add_executor_job(
            poll_api,
            self.hass,
            self.config_entry,
            self.proxmox,
            api_path,
            ProxmoxType.LXC,
            self.resource_id,
        )
        node_lxc = {}
        node_lxc["total"] = 0
        node_lxc["list"] = []
        for lxc in lxc_status:
            if lxc["status"] == "running":
                node_lxc["total"] += 1
                node_lxc["list"].append(f"{lxc['name']} ({lxc['vmid']})")
        api_status["lxc"] = node_lxc

        return ProxmoxNodeData(
            type=ProxmoxType.Node,
            model=api_status["cpuinfo"]["model"],
            status=api_status["status"],
            version=api_status["version"]["version"],
            uptime=api_status["uptime"],
            cpu=api_status["cpu"],
            disk_total=api_status["disk_max"],
            disk_used=api_status["disk_used"],
            memory_total=api_status["memory"]["total"],
            memory_used=api_status["memory"]["used"],
            memory_free=api_status["memory"]["free"],
            swap_total=api_status["swap"]["total"],
            swap_free=api_status["swap"]["free"],
            swap_used=api_status["swap"]["used"],
            qemu_on=api_status["qemu"]["total"],
            qemu_on_list=api_status["qemu"]["list"],
            lxc_on=api_status["lxc"]["total"],
            lxc_on_list=api_status["lxc"]["list"],
        )


class ProxmoxQEMUCoordinator(ProxmoxCoordinator):
    """Proxmox VE QEMU data update coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        proxmox: ProxmoxAPI,
        api_category: str,
        qemu_id: int,
    ) -> None:
        """Initialize the Proxmox QEMU coordinator."""

        super().__init__(
            hass,
            LOGGER,
            name=f"proxmox_coordinator_{api_category}_{qemu_id}",
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

        self.hass = hass
        self.config_entry: ConfigEntry = self.config_entry
        self.proxmox = proxmox
        self.node_name: str
        self.resource_id = qemu_id

    async def _async_update_data(self) -> ProxmoxVMData:
        """Update data  for Proxmox QEMU."""

        node_name = None
        api_status = None

        api_path = "cluster/resources"
        resources = await self.hass.async_add_executor_job(
            poll_api,
            self.hass,
            self.config_entry,
            self.proxmox,
            api_path,
            ProxmoxType.Resources,
            None,
        )

        for resource in resources:
            if "vmid" in resource:
                if int(resource["vmid"]) == int(self.resource_id):
                    node_name = resource["node"]

        if node_name is not None:
            api_path = f"nodes/{str(node_name)}/qemu/{self.resource_id}/status/current"
            api_status = await self.hass.async_add_executor_job(
                poll_api,
                self.hass,
                self.config_entry,
                self.proxmox,
                api_path,
                ProxmoxType.QEMU,
                self.resource_id,
            )
        else:
            raise UpdateFailed(f"{self.resource_id} QEMU node not found")

        if api_status is None or "status" not in api_status:
            raise UpdateFailed(f"QEMU {self.resource_id} unable to be found")

        update_device_via(self, ProxmoxType.QEMU, node_name)
        return ProxmoxVMData(
            type=ProxmoxType.QEMU,
            status=api_status["lock"]
            if ("lock" in api_status and api_status["lock"] == "suspended")
            else api_status["status"],
            name=api_status["name"],
            node=node_name,
            health=api_status["qmpstatus"],
            uptime=api_status["uptime"],
            cpu=api_status["cpu"],
            memory_total=api_status["maxmem"],
            memory_used=api_status["mem"],
            memory_free=api_status["maxmem"] - api_status["mem"],
            network_in=api_status["netin"],
            network_out=api_status["netout"],
            disk_total=api_status["maxdisk"],
            disk_used=api_status["disk"],
        )


class ProxmoxLXCCoordinator(ProxmoxCoordinator):
    """Proxmox VE LXC data update coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        proxmox: ProxmoxAPI,
        api_category: str,
        container_id: int,
    ) -> None:
        """Initialize the Proxmox LXC coordinator."""

        super().__init__(
            hass,
            LOGGER,
            name=f"proxmox_coordinator_{api_category}_{container_id}",
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

        self.hass = hass
        self.config_entry: ConfigEntry = self.config_entry
        self.proxmox = proxmox
        self.node_name: str
        self.resource_id = container_id

    async def _async_update_data(self) -> ProxmoxLXCData:
        """Update data  for Proxmox LXC."""

        node_name = None
        api_status = None

        api_path = "cluster/resources"
        resources = await self.hass.async_add_executor_job(
            poll_api,
            self.hass,
            self.config_entry,
            self.proxmox,
            api_path,
            ProxmoxType.Resources,
            None,
        )

        for resource in resources:
            if "vmid" in resource:
                if int(resource["vmid"]) == int(self.resource_id):
                    node_name = resource["node"]

        if node_name is not None:
            api_path = f"nodes/{str(node_name)}/lxc/{self.resource_id}/status/current"
            api_status = await self.hass.async_add_executor_job(
                poll_api,
                self.hass,
                self.config_entry,
                self.proxmox,
                api_path,
                ProxmoxType.LXC,
                self.resource_id,
            )
        else:
            raise UpdateFailed(f"{self.resource_id} LXC node not found")

        if api_status is None or "status" not in api_status:
            raise UpdateFailed(f"LXC {self.resource_id} unable to be found")

        update_device_via(self, ProxmoxType.LXC, node_name)
        return ProxmoxLXCData(
            type=ProxmoxType.LXC,
            status=api_status["status"],
            name=api_status["name"],
            node=node_name,
            uptime=api_status["uptime"],
            cpu=api_status["cpu"],
            memory_total=api_status["maxmem"],
            memory_used=api_status["mem"],
            memory_free=api_status["maxmem"] - api_status["mem"],
            network_in=api_status["netin"],
            network_out=api_status["netout"],
            disk_total=api_status["maxdisk"],
            disk_used=api_status["disk"],
            swap_total=api_status["maxswap"],
            swap_used=api_status["swap"],
            swap_free=api_status["maxswap"] - api_status["swap"],
        )


class ProxmoxStorageCoordinator(ProxmoxCoordinator):
    """Proxmox VE Storage data update coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        proxmox: ProxmoxAPI,
        api_category: str,
        storage_id: str,
    ) -> None:
        """Initialize the Proxmox Storage coordinator."""

        super().__init__(
            hass,
            LOGGER,
            name=f"proxmox_coordinator_{api_category}_{storage_id}",
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

        self.hass = hass
        self.config_entry: ConfigEntry = self.config_entry
        self.proxmox = proxmox
        self.node_name: str
        self.resource_id = storage_id

    async def _async_update_data(self) -> ProxmoxLXCData:
        """Update data  for Proxmox Update."""

        node_name = None
        api_status = None

        api_path = "cluster/resources"
        resources = await self.hass.async_add_executor_job(
            poll_api,
            self.hass,
            self.config_entry,
            self.proxmox,
            api_path,
            ProxmoxType.Resources,
            None,
        )

        for resource in resources:
            if "storage" in resource:
                if resource["storage"] == self.resource_id:
                    node_name = resource["node"]

        if node_name is not None:
            api_path = f"nodes/{str(node_name)}/storage/{self.resource_id}/status"
            api_status = await self.hass.async_add_executor_job(
                poll_api,
                self.hass,
                self.config_entry,
                self.proxmox,
                api_path,
                ProxmoxType.Storage,
                self.resource_id,
            )
        else:
            raise UpdateFailed(f"{self.resource_id} storage node not found")

        if api_status is None or "content" not in api_status:
            raise UpdateFailed(f"Storage {self.resource_id} unable to be found")

        update_device_via(self, ProxmoxType.Storage, node_name)
        return ProxmoxStorageData(
            type=ProxmoxType.Storage,
            node=node_name,
            disk_total=api_status["total"],
            disk_used=api_status["used"],
            disk_free=api_status["avail"],
            content=api_status["content"],
        )


class ProxmoxUpdateCoordinator(ProxmoxCoordinator):
    """Proxmox VE Update data update coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        proxmox: ProxmoxAPI,
        api_category: str,
        node_name: int,
    ) -> None:
        """Initialize the Proxmox Update coordinator."""

        super().__init__(
            hass,
            LOGGER,
            name=f"proxmox_coordinator_{api_category}_{node_name}",
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

        self.hass = hass
        self.config_entry: ConfigEntry = self.config_entry
        self.proxmox = proxmox
        self.node_name = node_name
        self.resource_id = f"{api_category.capitalize()} {node_name}"

    async def _async_update_data(self) -> ProxmoxUpdateData:
        """Update data  for Proxmox Update."""

        api_status = None

        if self.node_name is not None:
            api_path = f"nodes/{str(self.node_name)}/apt/update"
            api_status = await self.hass.async_add_executor_job(
                poll_api,
                self.hass,
                self.config_entry,
                self.proxmox,
                api_path,
                ProxmoxType.Update,
                self.resource_id,
            )
        else:
            raise UpdateFailed(f"{self.resource_id} node not found")

        if api_status is None:
            return ProxmoxUpdateData(
                type=ProxmoxType.Update,
                node=self.node_name,
                total=0,
                updates_list=[],
                update=False,
            )

        list = []
        total = 0
        for update in api_status:
            list.append(f"{update['Title']} - {update['Version']}")
            total += 1

        list.sort()

        update_avail = False
        if total > 0:
            update_avail = True

        return ProxmoxUpdateData(
            type=ProxmoxType.Update,
            node=self.node_name,
            total=total,
            updates_list=list,
            update=update_avail,
        )


class ProxmoxDiskCoordinator(ProxmoxCoordinator):
    """Proxmox VE Disk data update coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        proxmox: ProxmoxAPI,
        api_category: str,
        node_name: str,
        disk_id: str,
    ) -> None:
        """Initialize the Proxmox Disk coordinator."""

        super().__init__(
            hass,
            LOGGER,
            name=f"proxmox_coordinator_{api_category}_{node_name}_{disk_id}",
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

        self.hass = hass
        self.config_entry: ConfigEntry = self.config_entry
        self.proxmox = proxmox
        self.node_name = node_name
        self.resource_id = disk_id

    async def _async_update_data(self) -> ProxmoxDiskData:
        """Update data  for Proxmox Disk."""

        if self.node_name is not None:
            api_path = f"nodes/{str(self.node_name)}/disks/list"
            api_status = await self.hass.async_add_executor_job(
                poll_api,
                self.hass,
                self.config_entry,
                self.proxmox,
                api_path,
                ProxmoxType.Disk,
                self.resource_id,
            )
        else:
            raise UpdateFailed(f"{self.resource_id} node not found")

        if api_status is None:
            raise UpdateFailed("No data returned.")

        for disk in api_status:
            if disk["devpath"] == self.resource_id:
                disk_attributes = {}
                api_path = f"nodes/{self.node_name}/disks/smart?disk={self.resource_id}"
                disk_attributes_api = await self.hass.async_add_executor_job(
                    poll_api,
                    self.hass,
                    self.config_entry,
                    self.proxmox,
                    api_path,
                    ProxmoxType.Disk,
                    self.resource_id,
                )

                attributes_json = []
                if "attributes" in disk_attributes_api:
                    attributes_json = disk_attributes_api["attributes"]
                elif (
                    "type" in disk_attributes_api
                    and disk_attributes_api["type"] == "text"
                ):
                    attributes_text = disk_attributes_api["text"].split("\n")
                    for value_text in attributes_text:
                        value_json = value_text.split(":")
                        if len(value_json) >= 2:
                            attributes_json.append(
                                {
                                    "name": value_json[0].strip(),
                                    "raw": value_json[1].strip(),
                                }
                            )

                for disk_attribute in attributes_json:
                    if disk_attribute["name"] in ("Power_Cycle_Count", "Power Cycles"):
                        disk_attributes["power_cycles"] = disk_attribute["raw"]
                    elif disk_attribute["name"] in (
                        "Temperature_Celsius",
                        "Temperature",
                    ):
                        disk_attributes["temperature"] = disk_attribute["raw"].split(
                            " ", 1
                        )[0]

                return ProxmoxDiskData(
                    type=ProxmoxType.Disk,
                    node=self.node_name,
                    path=self.resource_id,
                    size=disk["size"] if "size" in disk else None,
                    health=disk["health"] if "health" in disk else None,
                    vendor=disk["vendor"] if "vendor" in disk else None,
                    serial=disk["serial"] if "serial" in disk else None,
                    model=disk["model"] if "model" in disk else None,
                    disk_rpm=disk["rpm"] if "rpm" in disk else None,
                    disk_type=disk["type"] if "type" in disk else None,
                    temperature=disk_attributes["temperature"]
                    if "temperature" in disk_attributes
                    else None,
                    power_cycles=disk_attributes["power_cycles"]
                    if "power_cycles" in disk_attributes
                    else None,
                )

        raise UpdateFailed(
            f"Disk {self.resource_id} not found on node {self.node_name}."
        )


def update_device_via(
    self,
    api_category: ProxmoxType,
    node_name: str,
) -> None:
    """Return the Device Info."""
    dev_reg = dr.async_get(self.hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=self.config_entry.entry_id,
        identifiers={
            (
                DOMAIN,
                f"{self.config_entry.entry_id}_{api_category.upper()}_{self.resource_id}",
            )
        },
    )
    via_device = dev_reg.async_get_device(
        {
            (
                DOMAIN,
                f"{self.config_entry.entry_id}_{ProxmoxType.Node.upper()}_{node_name}",
            )
        }
    )
    via_device_id: str | UndefinedType = via_device.id if via_device else UNDEFINED
    if device.via_device_id != via_device_id:
        LOGGER.debug(
            "Update device %s - connected via device: old=%s, new=%s",
            self.resource_id,
            device.via_device_id,
            via_device_id,
        )
        dev_reg.async_update_device(
            device.id,
            via_device_id=via_device_id,
            entry_type=dr.DeviceEntryType.SERVICE,
        )


def poll_api(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    proxmox: ProxmoxAPI,
    api_path: str,
    api_category: ProxmoxType,
    resource_id: str | int | None = None,
    issue_crete_permissions: bool | None = True,
) -> dict[str, Any] | None:
    """Return data from the Proxmox Node API."""

    def perssion_to_resource(
        api_category: ProxmoxType,
        resource_id: int | str,
    ):
        """Return the permissions required for the resource."""
        match api_category:
            case ProxmoxType.Node:
                return f"['perm','/nodes/{resource_id}',['Sys.Audit']]"
            case ProxmoxType.QEMU | ProxmoxType.LXC:
                return f"['perm','/vms/{resource_id}',['VM.Audit']]"
            case ProxmoxType.Storage:
                return f"['perm','/storage/{resource_id}',['Datastore.Audit'],'any',1]"
            case ProxmoxType.Update:
                return f"['perm','/nodes/{resource_id}',['Sys.Modify']]"
            case ProxmoxType.Disks:
                return f"['perm','/nodes/{resource_id}',['Sys.Audit']]"
            case _:
                return "Unmapped"

    try:
        return get_api(proxmox, api_path)
    except (
        AuthenticationError,
        SSLError,
        ConnectTimeout,
    ) as error:
        raise UpdateFailed(error) from error
    except ResourceException as error:
        if error.status_code == 403 and issue_crete_permissions:
            async_create_issue(
                hass,
                DOMAIN,
                f"{config_entry.entry_id}_{resource_id}_forbiden",
                is_fixable=False,
                severity=IssueSeverity.ERROR,
                translation_key="resource_exception_forbiden",
                translation_placeholders={
                    "resource": f"{api_category.capitalize()} {resource_id}",
                    "user": config_entry.data[CONF_USERNAME],
                    "permission": perssion_to_resource(api_category, resource_id),
                },
            )
            raise UpdateFailed(
                "User not allowed to access the resource, check user permissions as per the documentation, see details in the repair created by the integration."
            ) from error

    async_delete_issue(
        hass,
        DOMAIN,
        f"{config_entry.entry_id}_{resource_id}_forbiden",
    )
