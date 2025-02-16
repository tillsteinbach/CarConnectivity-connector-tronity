"""Module for vehicle classes."""
from __future__ import annotations
from typing import TYPE_CHECKING

from carconnectivity.vehicle import GenericVehicle, ElectricVehicle, CombustionVehicle, HybridVehicle
from carconnectivity.attributes import StringAttribute

if TYPE_CHECKING:
    from typing import Optional
    from carconnectivity.garage import Garage
    from carconnectivity_connectors.base.connector import BaseConnector


class TronityVehicle(GenericVehicle):  # pylint: disable=too-many-instance-attributes
    """
    A class to represent a generic Tronity vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[TronityVehicle] = None) -> None:
        if origin is not None:
            super().__init__(origin=origin)
            self.tronity_id: StringAttribute = origin.tronity_id
            self.tronity_id.parent = self
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)
            self.tronity_id: StringAttribute = StringAttribute(name='tronity_id', parent=self, tags={'connector_custom'})


class TronityElectricVehicle(ElectricVehicle, TronityVehicle):
    """
    Represents a Tronity electric vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[TronityVehicle] = None) -> None:
        if origin is not None:
            super().__init__(origin=origin)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)


class TronityCombustionVehicle(CombustionVehicle, TronityVehicle):
    """
    Represents a Tronity combustion vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[TronityVehicle] = None) -> None:
        if origin is not None:
            super().__init__(origin=origin)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)


class TronityHybridVehicle(HybridVehicle, TronityVehicle):
    """
    Represents a Tronity hybrid vehicle.
    """
    def __init__(self, vin: Optional[str] = None, garage: Optional[Garage] = None, managing_connector: Optional[BaseConnector] = None,
                 origin: Optional[TronityVehicle] = None) -> None:
        if origin is not None:
            super().__init__(origin=origin)
        else:
            super().__init__(vin=vin, garage=garage, managing_connector=managing_connector)
