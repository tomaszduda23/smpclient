"""A Bluetooth Low Energy (BLE) SMPTransport."""

import asyncio
import logging
import platform
import re
from typing import Final, List
from uuid import UUID

from bleak import BleakClient, BleakGATTCharacteristic, BleakScanner
from bleak.backends.device import BLEDevice
from smp import header as smphdr

from smpclient.exceptions import SMPClientException
from smpclient.transport import SMPTransport

SMP_SERVICE_UUID: Final = UUID("8D53DC1D-1DB7-4CD3-868B-8A527460AA84")
SMP_CHARACTERISTIC_UUID: Final = UUID("DA2E7828-FBCE-4E01-AE9E-261174997C48")

MAC_ADDRESS_PATTERN: Final = re.compile(r"([0-9A-F]{2}[:]){5}[0-9A-F]{2}$", flags=re.IGNORECASE)
UUID_PATTERN: Final = re.compile(
    r"^[a-f0-9]{8}-?[a-f0-9]{4}-?4[a-f0-9]{3}-?[89ab][a-f0-9]{3}-?[a-f0-9]{12}\Z",
    flags=re.IGNORECASE,
)


class SMPBLETransportException(SMPClientException):
    ...


class SMPBLETransportDeviceNotFound(SMPBLETransportException):
    ...


class SMPBLETransportNotSMPServer(SMPBLETransportException):
    ...


logger = logging.getLogger(__name__)


class SMPBLETransport(SMPTransport):
    def __init__(self) -> None:
        self._buffer = bytearray()
        self._notify_condition = asyncio.Condition()
        logger.debug(f"Initialized {self.__class__.__name__}")

    async def connect(self, address: str) -> None:
        logger.debug(f"Scanning for {address=}")
        device: BLEDevice | None = (
            await BleakScanner.find_device_by_address(address)  # type: ignore # upstream fix
            if MAC_ADDRESS_PATTERN.match(address) or UUID_PATTERN.match(address)
            else await BleakScanner.find_device_by_name(address)  # type: ignore # upstream fix
        )

        if type(device) is BLEDevice:
            self._client = BleakClient(device, services=(str(SMP_SERVICE_UUID),))
        else:
            raise SMPBLETransportDeviceNotFound(f"Device '{address}' not found")

        logger.debug(f"Found device: {device=}, connecting...")
        await self._client.connect()
        logger.debug(f"Connected to {device=}")

        smp_characteristic = self._client.services.get_characteristic(SMP_CHARACTERISTIC_UUID)
        if smp_characteristic is None:
            raise SMPBLETransportNotSMPServer("Missing the SMP characteristic UUID.")
        else:
            logger.debug(f"Found SMP characteristic: {smp_characteristic=}")
            logger.info(f"{smp_characteristic.max_write_without_response_size=}")
            if (
                platform.system() == "Windows"
                and smp_characteristic.max_write_without_response_size == 20
            ):
                # https://github.com/hbldh/bleak/pull/1552#issuecomment-2105573291
                logger.warning(
                    "The SMP characteristic MTU is 20 bytes, possibly a Windows bug, checking again"
                )
                await asyncio.sleep(2)
                smp_characteristic._max_write_without_response_size = (
                    self._client._backend._session.max_pdu_size - 3  # type: ignore
                )
                logger.warning(f"{smp_characteristic.max_write_without_response_size=}")

            self._smp_characteristic = smp_characteristic

        logger.debug(f"Starting notify on {SMP_CHARACTERISTIC_UUID=}")
        await self._client.start_notify(SMP_CHARACTERISTIC_UUID, self._notify_callback)
        logger.debug(f"Started notify on {SMP_CHARACTERISTIC_UUID=}")

    async def disconnect(self) -> None:
        logger.debug(f"Disonnecting from {self._client.address}")
        await self._client.disconnect()
        logger.debug(f"Disconnected from {self._client.address}")

    async def send(self, data: bytes) -> None:
        logger.debug(f"Sending {len(data)} bytes, {self.mtu=}")
        for offset in range(0, len(data), self.mtu):
            await self._client.write_gatt_char(
                self._smp_characteristic, data[offset : offset + self.mtu], response=False
            )
        logger.debug(f"Sent {len(data)} bytes")

    async def receive(self) -> bytes:
        # Note: self._buffer is mutated asynchronously by this method and self._notify_callback().
        #       self._notify_condition is used to synchronize access to self._buffer.

        async with self._notify_condition:  # wait for the header
            logger.debug(f"Waiting for notify on {SMP_CHARACTERISTIC_UUID=}")
            await self._notify_condition.wait()

            if len(self._buffer) < smphdr.Header.SIZE:  # pragma: no cover
                raise SMPBLETransportException(
                    f"Buffer contents not big enough for SMP header: {self._buffer=}"
                )

            header: Final = smphdr.Header.loads(self._buffer[: smphdr.Header.SIZE])
            logger.debug(f"Received {header=}")

        message_length: Final = header.length + header.SIZE
        logger.debug(f"Waiting for the rest of the {message_length} byte response")

        while True:  # wait for the rest of the message
            async with self._notify_condition:
                if len(self._buffer) == message_length:
                    logger.debug(f"Finished receiving {message_length} byte response")
                    out = bytes(self._buffer)
                    self._buffer.clear()
                    return out
                elif len(self._buffer) > message_length:  # pragma: no cover
                    raise SMPBLETransportException("Length of buffer passed expected message size.")
                await self._notify_condition.wait()

    async def _notify_callback(self, sender: BleakGATTCharacteristic, data: bytes) -> None:
        if sender.uuid != str(SMP_CHARACTERISTIC_UUID):  # pragma: no cover
            raise SMPBLETransportException(f"Unexpected notify from {sender}; {data=}")
        async with self._notify_condition:
            logger.debug(f"Received {len(data)} bytes from {SMP_CHARACTERISTIC_UUID=}")
            self._buffer.extend(data)
            self._notify_condition.notify()

    async def send_and_receive(self, data: bytes) -> bytes:
        await self.send(data)
        return await self.receive()

    @property
    def mtu(self) -> int:
        return self._smp_characteristic.max_write_without_response_size

    @staticmethod
    async def scan(timeout: int = 5) -> List[BLEDevice]:
        """Scan for BLE devices."""
        logger.debug(f"Scanning for BLE devices for {timeout} seconds")
        devices: Final = await BleakScanner(service_uuids=[str(SMP_SERVICE_UUID)]).discover(
            timeout=timeout, return_adv=True
        )
        smp_servers: Final = [
            d for d, a in devices.values() if SMP_SERVICE_UUID in {UUID(u) for u in a.service_uuids}
        ]
        logger.debug(f"Found {len(smp_servers)} SMP devices: {smp_servers=}")
        return smp_servers
