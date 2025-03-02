"""Module implements the connector to interact with the Skoda API."""
from __future__ import annotations
from typing import TYPE_CHECKING

import threading

import os
import traceback
import logging
import netrc
from datetime import datetime, timezone, timedelta
import requests

from carconnectivity.garage import Garage
from carconnectivity.errors import AuthenticationError, TooManyRequestsError, RetrievalError, APIError, APICompatibilityError, \
    TemporaryAuthenticationError, CommandError
from carconnectivity.util import robust_time_parse, log_extra_keys, config_remove_credentials
from carconnectivity.drive import ElectricDrive, GenericDrive
from carconnectivity.units import Power, Length
from carconnectivity.charging import ChargingConnector, Charging
from carconnectivity.attributes import DurationAttribute, EnumAttribute
from carconnectivity.commands import Commands
from carconnectivity.command_impl import ChargingStartStopCommand
from carconnectivity.enums import ConnectionState

from carconnectivity_connectors.base.connector import BaseConnector
from carconnectivity_connectors.tronity.vehicle import TronityElectricVehicle
from carconnectivity_connectors.tronity.auth.session_manager import SessionManager, SessionCredentials, Service
from carconnectivity_connectors.tronity.auth.tronity_session import TronitySession
from carconnectivity_connectors.tronity._version import __version__


if TYPE_CHECKING:
    from typing import Dict, List, Optional, Any, Union

    from carconnectivity.carconnectivity import CarConnectivity

LOG: logging.Logger = logging.getLogger("carconnectivity.connectors.tronity")
LOG_API: logging.Logger = logging.getLogger("carconnectivity.connectors.tronity-api-debug")


# pylint: disable=too-many-lines
class Connector(BaseConnector):
    """
    Connector class for Skoda API connectivity.
    Args:
        car_connectivity (CarConnectivity): An instance of CarConnectivity.
        config (Dict): Configuration dictionary containing connection details.
    Attributes:
        max_age (Optional[int]): Maximum age for cached data in seconds.
    """
    def __init__(self, connector_id: str, car_connectivity: CarConnectivity, config: Dict) -> None:
        BaseConnector.__init__(self, connector_id=connector_id, car_connectivity=car_connectivity, config=config, log=LOG, api_log=LOG_API)

        self._background_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self.connection_state: EnumAttribute = EnumAttribute(name="connection_state", parent=self, value_type=ConnectionState,
                                                             value=ConnectionState.DISCONNECTED, tags={'connector_custom'})
        self.interval: DurationAttribute = DurationAttribute(name="interval", parent=self, tags={'connector_custom'})
        self.interval.minimum = timedelta(seconds=180)
        self.interval._is_changeable = True  # pylint: disable=protected-access

        self.commands: Commands = Commands(parent=self)

        LOG.info("Loading tronity connector with config %s", config_remove_credentials(config))

        self.active_config['client_id'] = None
        self.active_config['client_secret'] = None
        if 'client_id' in config and 'client_secret' in config:
            self.active_config['client_id'] = config['client_id']
            self.active_config['client_secret'] = config['client_secret']
        else:
            if 'netrc' in config:
                self.active_config['netrc'] = config['netrc']
            else:
                self.active_config['netrc'] = os.path.join(os.path.expanduser("~"), ".netrc")
            try:
                secrets = netrc.netrc(file=self.active_config['netrc'])
                secret: tuple[str, str, str] | None = secrets.authenticators("Tronity")
                if secret is None:
                    raise AuthenticationError(f'Authentication using {self.active_config["netrc"]} failed: volkswagen not found in netrc')
                self.active_config['client_id'], _, self.active_config['client_secret'] = secret

            except netrc.NetrcParseError as err:
                LOG.error('Authentification using %s failed: %s', self.active_config['netrc'], err)
                raise AuthenticationError(f'Authentication using {self.active_config["netrc"]} failed: {err}') from err
            except TypeError as err:
                if 'client_id' not in config:
                    raise AuthenticationError(f'"Tronity" entry was not found in {self.active_config["netrc"]} netrc-file.'
                                              ' Create it or provide client_id and client_secret in config') from err
            except FileNotFoundError as err:
                raise AuthenticationError(f'{self.active_config["netrc"]} netrc-file was not found. Create it or provide client_id'
                                          ' and client_secret in config') from err

        self.active_config['interval'] = 180
        if 'interval' in config:
            self.active_config['interval'] = config['interval']
            if self.active_config['interval'] < 60:
                raise ValueError('Intervall must be at least 60 seconds')
        self.interval._set_value(value=timedelta(seconds=self.active_config['interval']))
        self.active_config['max_age'] = self.active_config['interval'] - 1

        if self.active_config['client_id'] is None or self.active_config['client_secret'] is None:
            raise AuthenticationError('client_id or client_secret not provided')

        self._manager: SessionManager = SessionManager(tokenstore=car_connectivity.get_tokenstore(), cache=car_connectivity.get_cache())
        session: requests.Session = self._manager.get_session(Service.TRONITY, SessionCredentials(client_id=self.active_config['client_id'],
                                                                                                  client_secret=self.active_config['client_secret']))
        if not isinstance(session, TronitySession):
            raise AuthenticationError('Could not create session')
        self.session: TronitySession = session
        self.session.retries = 3
        self.session.timeout = 180
        self.session.refresh()

        self._elapsed: List[timedelta] = []

    def startup(self) -> None:
        self._background_thread = threading.Thread(target=self._background_loop, daemon=False)
        self._background_thread.name = 'carconnectivity.connectors.tronity-background'
        self._background_thread.start()
        self.healthy._set_value(value=True)  # pylint: disable=protected-access

    def _background_loop(self) -> None:
        self._stop_event.clear()
        while not self._stop_event.is_set():
            interval = 300
            try:
                try:
                    self.fetch_all()
                    self.last_update._set_value(value=datetime.now(tz=timezone.utc))  # pylint: disable=protected-access
                    if self.interval.value is not None:
                        interval: float = self.interval.value.total_seconds()
                except Exception:
                    self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                    if self.interval.value is not None:
                        interval: float = self.interval.value.total_seconds()
                    raise
            except TooManyRequestsError as err:
                LOG.error('Retrieval error during update. Too many requests from your account (%s). Will try again after 15 minutes', str(err))
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self._stop_event.wait(900)
            except RetrievalError as err:
                LOG.error('Retrieval error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self._stop_event.wait(interval)
            except APICompatibilityError as err:
                LOG.error('API compatability error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self._stop_event.wait(interval)
            except TemporaryAuthenticationError as err:
                LOG.error('Temporary authentification error during update (%s). Will try again after configured interval of %ss', str(err), interval)
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self._stop_event.wait(interval)
            except Exception as err:
                LOG.critical('Critical error during update: %s', traceback.format_exc())
                self.connection_state._set_value(value=ConnectionState.ERROR)  # pylint: disable=protected-access
                self.healthy._set_value(value=False)  # pylint: disable=protected-access
                raise err
            else:
                self.connection_state._set_value(value=ConnectionState.CONNECTED)  # pylint: disable=protected-access
                self._stop_event.wait(interval)

    def persist(self) -> None:
        """
        Persists the current state using the manager's persist method.

        This method calls the `persist` method of the `_manager` attribute to save the current state.
        """
        self._manager.persist()

    def shutdown(self) -> None:
        """
        Shuts down the connector by persisting current state, closing the session,
        and cleaning up resources.

        This method performs the following actions:
        1. Persists the current state.
        2. Closes the session.
        3. Sets the session and manager to None.
        4. Calls the shutdown method of the base connector.

        Returns:
            None
        """
        # Disable and remove all vehicles managed soley by this connector
        for vehicle in self.car_connectivity.garage.list_vehicles():
            if len(vehicle.managing_connectors) == 1 and self in vehicle.managing_connectors:
                self.car_connectivity.garage.remove_vehicle(vehicle.id)
                vehicle.enabled = False
        self._stop_event.set()
        if self._background_thread is not None:
            self._background_thread.join()
        self.persist()
        self.session.close()
        BaseConnector.shutdown(self)

    def fetch_all(self) -> None:
        """
        Fetches all necessary data for the connector.

        This method calls the `fetch_vehicles` method to retrieve vehicle data.
        """
        self.fetch_vehicles()
        self.car_connectivity.transaction_end()

    def fetch_vehicles(self) -> None:
        """
        Fetches the list of vehicles from the Skoda Connect API and updates the garage with new vehicles.
        This method sends a request to the Skoda Connect API to retrieve the list of vehicles associated with the user's account.
        If new vehicles are found in the response, they are added to the garage.

        Returns:
            None
        """
        garage: Garage = self.car_connectivity.garage
        url = 'https://api.tronity.tech/tronity/vehicles'
        data: Dict[str, Any] | None = self._fetch_data(url, session=self.session)
        seen_vehicle_vins: set[str] = set()
        if data is not None and 'data' in data and data['data'] is not None:
            for vehicle_dict in data['data']:
                if 'vin' in vehicle_dict and vehicle_dict['vin'] is not None:
                    seen_vehicle_vins.add(vehicle_dict['vin'])
                    vehicle: Optional[TronityElectricVehicle] = garage.get_vehicle(vehicle_dict['vin'])  # pyright: ignore[reportAssignmentType]
                    if not vehicle:
                        vehicle = TronityElectricVehicle(vin=vehicle_dict['vin'], garage=garage, managing_connector=self)
                        garage.add_vehicle(vehicle_dict['vin'], vehicle)

                    captured_at = None
                    if 'updatedAt' in vehicle_dict and vehicle_dict['updatedAt'] is not None:
                        captured_at = robust_time_parse(vehicle_dict['updatedAt'])

                    if 'id' in vehicle_dict and vehicle_dict['id'] is not None:
                        vehicle.tronity_id._set_value(vehicle_dict['id'], measured=captured_at)  # pylint: disable=protected-access

                    if 'displayName' in vehicle_dict and vehicle_dict['displayName'] is not None:
                        vehicle.name._set_value(vehicle_dict['displayName'], measured=captured_at)  # pylint: disable=protected-access
                    else:
                        vehicle.name._set_value(None)  # pylint: disable=protected-access

                    if 'model' in vehicle_dict and vehicle_dict['model'] is not None:
                        vehicle.model._set_value(vehicle_dict['model'], measured=captured_at)  # pylint: disable=protected-access
                    else:
                        vehicle.model._set_value(None)  # pylint: disable=protected-access

                    if 'manufacture' in vehicle_dict and vehicle_dict['manufacture'] is not None:
                        vehicle.manufacturer._set_value(vehicle_dict['manufacture'], measured=captured_at)  # pylint: disable=protected-access
                    else:
                        vehicle.manufacturer._set_value(None)  # pylint: disable=protected-access

                    log_extra_keys(LOG_API, 'vehicles', vehicle_dict,  {'vin', 'displayName', 'id', 'name', 'model', 'manufacture', 'scopes', 'updatedAt',
                                                                        'valid', 'year', 'createdAt'})

                    vehicle = self.fetch_vehicle_status(vehicle)
                else:
                    raise APIError('Could not parse vehicle, vin missing')
        for vin in set(garage.list_vehicle_vins()) - seen_vehicle_vins:
            vehicle_to_remove = garage.get_vehicle(vin)
            if vehicle_to_remove is not None and vehicle_to_remove.is_managed_by_connector(self):
                garage.remove_vehicle(vin)

    def fetch_vehicle_status(self, vehicle: TronityElectricVehicle) -> TronityElectricVehicle:
        """
        Fetches the latest status of the given vehicle from the Tronity API and updates the vehicle object with the retrieved data.

        Args:
            vehicle (TronityElectricVehicle): The vehicle object to update with the latest status.

        Returns:
            TronityElectricVehicle: The updated vehicle object.

        Raises:
            APIError: If the vehicle does not have a valid tronity_id.

        Updates the following attributes of the vehicle object based on the fetched data:
            - odometer
            - total_range
            - level
            - charging state
            - charging commands
            - connector connection state
            - charging power
            - position (latitude and longitude)
        """
        if not vehicle.tronity_id.enabled or vehicle.tronity_id.value is None:
            raise APIError('Vehicle does not have a tronity_id')
        tronity_id = vehicle.tronity_id.value
        url = f'https://api.tronity.tech/tronity/vehicles/{tronity_id}/last_record'
        data: Dict[str, Any] | None = self._fetch_data(url, session=self.session)
        if data is not None:
            timestamp: Optional[datetime] = None
            if 'timestamp' in data and data['timestamp'] is not None:
                timestamp = datetime.fromtimestamp(data['timestamp'] / 1000, tz=timezone.utc)
            if 'odometer' in data and data['odometer'] is not None:
                vehicle.odometer._set_value(data['odometer'], measured=timestamp)  # pylint: disable=protected-access
            else:
                vehicle.odometer._set_value(None)  # pylint: disable=protected-access
            if 'range' in data and data['range'] is not None:
                vehicle.drives.total_range._set_value(value=data['range'], measured=timestamp, unit=Length.KM)  # pylint: disable=protected-access
            else:
                vehicle.drives.total_range._set_value(None)  # pylint: disable=protected-access
            if 'level' in data and data['level'] is not None:
                drive: Optional[ElectricDrive] = vehicle.get_electric_drive()
                if drive is None:
                    drive = ElectricDrive(drive_id='primary', drives=vehicle.drives)
                    drive.type._set_value(GenericDrive.Type.ELECTRIC)  # pylint: disable=protected-access
                    vehicle.drives.add_drive(drive)

                drive.level._set_value(data['level'], measured=timestamp)  # pylint: disable=protected-access
            else:
                drive: Optional[ElectricDrive] = vehicle.get_electric_drive()
                if drive is not None:
                    drive.level._set_value(None)  # pylint: disable=protected-access
            if 'charging' in data and data['charging'] is not None:
                if data['charging'] == 'Error':
                    vehicle.charging.state._set_value(Charging.ChargingState.ERROR, measured=timestamp)  # pylint: disable=protected-access
                elif data['charging'] == 'Charging':
                    vehicle.charging.state._set_value(Charging.ChargingState.CHARGING, measured=timestamp)  # pylint: disable=protected-access
                elif data['charging'] == 'Disconnected':
                    vehicle.charging.state._set_value(Charging.ChargingState.OFF, measured=timestamp)  # pylint: disable=protected-access
                else:
                    LOG_API.warning('Unknown charging state: %s', data['charging'])
                    vehicle.charging.state._set_value(Charging.ChargingState.UNKNOWN, measured=timestamp)  # pylint: disable=protected-access

                if not vehicle.charging.commands.contains_command('start-stop'):
                    start_stop_command: ChargingStartStopCommand = ChargingStartStopCommand(parent=vehicle.charging.commands)
                    start_stop_command._add_on_set_hook(self.__on_charging_start_stop)  # pylint: disable=protected-access
                    start_stop_command.enabled = True
                    vehicle.charging.commands.add_command(start_stop_command)
            else:
                vehicle.charging.state._set_value(None)  # pylint: disable=protected-access
            if 'plugged' in data and data['plugged'] is not None:
                if data['plugged']:
                    vehicle.charging.connector.connection_state._set_value(  # pylint: disable=protected-access
                        ChargingConnector.ChargingConnectorConnectionState.CONNECTED, measured=timestamp)
                else:
                    vehicle.charging.connector.connection_state._set_value(  # pylint: disable=protected-access
                        ChargingConnector.ChargingConnectorConnectionState.DISCONNECTED, measured=timestamp)
            else:
                vehicle.charging.connector.connection_state._set_value(None)  # pylint: disable=protected-access
            if 'chargerPower' in data and data['chargerPower'] is not None:
                vehicle.charging.power._set_value(data['chargerPower'], measured=timestamp, unit=Power.KW)  # pylint: disable=protected-access
            else:
                vehicle.charging.power._set_value(None)  # pylint: disable=protected-access
            if 'chargeRemainingTime' in data and data['chargeRemainingTime'] is not None:
                estimated_date_reached: datetime = datetime.now(tz=timezone.utc) + timedelta(minutes=data['chargeRemainingTime'])
                vehicle.charging.estimated_date_reached._set_value(estimated_date_reached, measured=timestamp)  # pylint: disable=protected-access
            else:
                vehicle.charging.estimated_date_reached._set_value(None)  # pylint: disable=protected-access
            if 'latitude' in data and data['latitude'] is not None:
                vehicle.position.latitude._set_value(data['latitude'], measured=timestamp)  # pylint: disable=protected-access
            else:
                vehicle.position.latitude._set_value(None)  # pylint: disable=protected-access
            if 'longitude' in data and data['longitude'] is not None:
                vehicle.position.longitude._set_value(data['longitude'], measured=timestamp)  # pylint: disable=protected-access
            else:
                vehicle.position.longitude._set_value(None)  # pylint: disable=protected-access
            log_extra_keys(LOG_API, 'last_record', data,  {'odometer', 'range', 'level', 'charging', 'plugged', 'chargerPower', 'chargeRemainingTime',
                                                           'latitude', 'longitude', 'timestamp', 'lastUpdate'})
        return vehicle

    def _record_elapsed(self, elapsed: timedelta) -> None:
        """
        Records the elapsed time.

        Args:
            elapsed (timedelta): The elapsed time to record.
        """
        self._elapsed.append(elapsed)

    def _fetch_data(self, url, session, allow_empty=False, allow_http_error=False, allowed_errors=None) -> Optional[Dict[str, Any]]:  # noqa: C901
        data: Optional[Dict[str, Any]] = None
        try:
            status_response: requests.Response = session.get(url, allow_redirects=False)
            self._record_elapsed(status_response.elapsed)
            if status_response.status_code in (requests.codes['ok'], requests.codes['multiple_status']):
                data = status_response.json()
            elif status_response.status_code == requests.codes['too_many_requests']:
                raise TooManyRequestsError('Could not fetch data due to too many requests from your account. '
                                           f'Status Code was: {status_response.status_code}')
            elif status_response.status_code == requests.codes['unauthorized']:
                LOG.info('Server asks for new authorization')
                session.login()
                status_response = session.get(url, allow_redirects=False)

                if status_response.status_code in (requests.codes['ok'], requests.codes['multiple_status']):
                    data = status_response.json()
                elif not allow_http_error or (allowed_errors is not None and status_response.status_code not in allowed_errors):
                    raise RetrievalError(f'Could not fetch data even after re-authorization. Status Code was: {status_response.status_code}')
            elif not allow_http_error or (allowed_errors is not None and status_response.status_code not in allowed_errors):
                raise RetrievalError(f'Could not fetch data. Status Code was: {status_response.status_code}')
        except requests.exceptions.ConnectionError as connection_error:
            raise RetrievalError(f'Connection error: {connection_error}') from connection_error
        except requests.exceptions.ChunkedEncodingError as chunked_encoding_error:
            raise RetrievalError(f'Error: {chunked_encoding_error}') from chunked_encoding_error
        except requests.exceptions.ReadTimeout as timeout_error:
            raise RetrievalError(f'Timeout during read: {timeout_error}') from timeout_error
        except requests.exceptions.RetryError as retry_error:
            raise RetrievalError(f'Retrying failed: {retry_error}') from retry_error
        except requests.exceptions.JSONDecodeError as json_error:
            if allow_empty:
                data = None
            else:
                raise RetrievalError(f'JSON decode error: {json_error}') from json_error
        return data

    def get_version(self) -> str:
        return __version__

    def get_type(self) -> str:
        return "carconnectivity-connector-tronity"

    def __on_charging_start_stop(self, start_stop_command: ChargingStartStopCommand, command_arguments: Union[str, Dict[str, Any]]) \
            -> Union[str, Dict[str, Any]]:
        if start_stop_command.parent is None or start_stop_command.parent.parent is None \
                or start_stop_command.parent.parent.parent is None or not isinstance(start_stop_command.parent.parent.parent, TronityElectricVehicle):
            raise CommandError('Object hierarchy is not as expected')
        if not isinstance(command_arguments, dict):
            raise CommandError('Command arguments are not a dictionary')
        vehicle: TronityElectricVehicle = start_stop_command.parent.parent.parent
        tronity_id: Optional[str] = vehicle.tronity_id.value
        if tronity_id is None:
            raise CommandError('tronity_id in object hierarchy missing')
        if 'command' not in command_arguments:
            raise CommandError('Command argument missing')
        if command_arguments['command'] == ChargingStartStopCommand.Command.START:
            url = f'https://api.tronity.tech/tronity/vehicles/{tronity_id}/control/start_charging'
            command_response: requests.Response = self.session.post(url, allow_redirects=True)
        elif command_arguments['command'] == ChargingStartStopCommand.Command.STOP:
            url = f'https://api.tronity.tech/tronity/vehicles/{tronity_id}/control/start_charging'
            command_response: requests.Response = self.session.post(url, allow_redirects=True)
        else:
            raise CommandError(f'Unknown command {command_arguments["command"]}')

        if command_response.status_code != requests.codes['ok']:
            if command_response.status_code == requests.codes['method_not_allowed']:
                LOG.error('Could not start/stop charging, not supported by tronity for this vehicle (%s: %s)', command_response.status_code,
                          command_response.text)
                raise CommandError(f'Could not start/stop charging, not supported by tronity for this vehicle '
                                   f'({command_response.status_code}: {command_response.text})')
            elif command_response.status_code == requests.codes['conflict']:
                LOG.error('Could not start/stop charging, the vehicle my be unreachable(%s: %s)', command_response.status_code,
                          command_response.text)
                raise CommandError(f'Could not start/stop charging, the vehicle my be unreachable '
                                   f'({command_response.status_code}: {command_response.text})')
            LOG.error('Could not start/stop charging (%s: %s)', command_response.status_code, command_response.text)
            raise CommandError(f'Could not start/stop charging ({command_response.status_code}: {command_response.text})')
        return command_arguments

    def get_name(self) -> str:
        return "Tronity Connector"
