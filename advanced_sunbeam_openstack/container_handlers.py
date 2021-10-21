# Copyright 2021 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Base classes for defining Pebble handlers.

The PebbleHandler defines the pebble layers, manages pushing
configuration to the containers and managing the service running
in the container.
"""

import logging

import advanced_sunbeam_openstack.core as sunbeam_core
import advanced_sunbeam_openstack.templating as sunbeam_templating
import advanced_sunbeam_openstack.cprocess as sunbeam_cprocess
import ops.charm

from collections.abc import Callable
from typing import List

logger = logging.getLogger(__name__)


class PebbleHandler(ops.charm.Object):
    """Base handler for Pebble based containers."""

    _state = ops.framework.StoredState()

    def __init__(
        self,
        charm: ops.charm.CharmBase,
        container_name: str,
        service_name: str,
        container_configs: List[sunbeam_core.ContainerConfigFile],
        template_dir: str,
        openstack_release: str,
        callback_f: Callable,
    ) -> None:
        """Run constructor."""
        super().__init__(charm, None)
        self._state.set_default(pebble_ready=False)
        self._state.set_default(config_pushed=False)
        self._state.set_default(service_ready=False)
        self.charm = charm
        self.container_name = container_name
        self.service_name = service_name
        self.container_configs = container_configs
        self.container_configs.extend(self.default_container_configs())
        self.template_dir = template_dir
        self.openstack_release = openstack_release
        self.callback_f = callback_f
        self.setup_pebble_handler()

    def setup_pebble_handler(self) -> None:
        """Configure handler for pebble ready event."""
        prefix = self.container_name.replace("-", "_")
        pebble_ready_event = getattr(self.charm.on, f"{prefix}_pebble_ready")
        self.framework.observe(
            pebble_ready_event, self._on_service_pebble_ready
        )

    def _on_service_pebble_ready(
        self, event: ops.charm.PebbleReadyEvent
    ) -> None:
        """Handle pebble ready event."""
        container = event.workload
        container.add_layer(self.service_name, self.get_layer(), combine=True)
        logger.debug(f"Plan: {container.get_plan()}")
        self.ready = True
        self._state.pebble_ready = True
        self.charm.configure_charm(event)

    def write_config(self, context: sunbeam_core.OPSCharmContexts) -> None:
        """Write configuration files into the container.

        On the pre-condition that all relation adapters are ready
        for use, write all configuration files into the container
        so that the underlying service may be started.
        """
        container = self.charm.unit.get_container(self.container_name)
        if container:
            sunbeam_templating.sidecar_config_render(
                [container],
                self.container_configs,
                self.template_dir,
                self.openstack_release,
                context,
            )
            self._state.config_pushed = True
        else:
            logger.debug("Container not ready")

    def get_layer(self) -> dict:
        """Pebble configuration layer for the container."""
        return {}

    def init_service(self, context: sunbeam_core.OPSCharmContexts) -> None:
        """Initialise service ready for use.

        Write configuration files to the container and record
        that service is ready for us.
        """
        self.write_config(context)
        self._state.service_ready = True

    def default_container_configs(
        self,
    ) -> List[sunbeam_core.ContainerConfigFile]:
        """Generate default container configurations.

        These should be used by all inheriting classes and are
        automatically added to the list or container configurations
        provided during object instantiation.
        """
        return []

    @property
    def pebble_ready(self) -> bool:
        """Determine if pebble is running and ready for use."""
        return self._state.pebble_ready

    @property
    def config_pushed(self) -> bool:
        """Determine if configuration has been pushed to the container."""
        return self._state.config_pushed

    @property
    def service_ready(self) -> bool:
        """Determine whether the service the container provides is running."""
        return self._state.service_ready


class WSGIPebbleHandler(PebbleHandler):
    """WSGI oriented handler for a Pebble managed container."""

    def __init__(
        self,
        charm: ops.charm.CharmBase,
        container_name: str,
        service_name: str,
        container_configs: List[sunbeam_core.ContainerConfigFile],
        template_dir: str,
        openstack_release: str,
        callback_f: Callable,
        wsgi_service_name: str,
    ) -> None:
        """Run constructor."""
        super().__init__(
            charm,
            container_name,
            service_name,
            container_configs,
            template_dir,
            openstack_release,
            callback_f,
        )
        self.wsgi_service_name = wsgi_service_name

    def start_wsgi(self) -> None:
        """Start WSGI service."""
        container = self.charm.unit.get_container(self.container_name)
        if not container:
            logger.debug(
                f"{self.container_name} container is not ready. "
                "Cannot start wgi service."
            )
            return
        service = container.get_service(self.wsgi_service_name)
        if service.is_running():
            container.stop(self.wsgi_service_name)

        container.start(self.wsgi_service_name)

    def get_layer(self) -> dict:
        """Apache WSGI service pebble layer.

        :returns: pebble layer configuration for wsgi service
        """
        return {
            "summary": f"{self.service_name} layer",
            "description": "pebble config layer for apache wsgi",
            "services": {
                f"{self.wsgi_service_name}": {
                    "override": "replace",
                    "summary": f"{self.service_name} wsgi",
                    "command": "/usr/sbin/apache2ctl -DFOREGROUND",
                    "startup": "disabled",
                },
            },
        }

    def init_service(self, context: sunbeam_core.OPSCharmContexts) -> None:
        """Enable and start WSGI service."""
        container = self.charm.unit.get_container(self.container_name)
        self.write_config(context)
        try:
            sunbeam_cprocess.check_output(
                container, f"a2ensite {self.wsgi_service_name} && sleep 1"
            )
        except sunbeam_cprocess.ContainerProcessError:
            logger.exception(
                f"Failed to enable {self.wsgi_service_name} site in apache"
            )
            # ignore for now - pebble is raising an exited too quickly, but it
            # appears to work properly.
        self.start_wsgi()
        self._state.service_ready = True

    @property
    def wsgi_conf(self) -> str:
        """Location of WSGI config file."""
        return f"/etc/apache2/sites-available/wsgi-{self.service_name}.conf"

    def default_container_configs(
        self,
    ) -> List[sunbeam_core.ContainerConfigFile]:
        """Container configs for WSGI service."""
        return [
            sunbeam_core.ContainerConfigFile(
                [self.container_name], self.wsgi_conf, "root", "root"
            )
        ]
