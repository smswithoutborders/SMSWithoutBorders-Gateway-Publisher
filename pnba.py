"""
Phone number-based Authentication Client Module.
"""

import logging
import re
import asyncio
import telegram_client

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("[PNBA Client]")


class PNBAClient:
    """
    PNBA client implementation.
    """

    def __init__(self, platform_name, phone_number) -> None:
        """
        Initialize the PNBAClient.

        Args:
            platform_name (str): The name of the platform (e.g., 'telegram').
        """

        self.platform = platform_name.lower()
        self.phone_number = re.sub(r"\s+", "", phone_number)
        self.session = telegram_client if self.platform == "telegram" else None

    def authorization(self):
        """
        Send an authorization request to the platform.

        Returns:
            dict: A dictionary containing the response message or error.
        """
        try:
            client = self.session.Methods(self.phone_number)
            asyncio.run(client.authorize())

            return {
                "response": f"Successfully sent authorization to your {self.platform} app."
            }

        except (
            self.session.Errors.SessionExistError,
            self.session.Errors.FloodWaitError,
            self.session.Errors.RPCError,
        ) as e:
            return {"error": str(e)}

    def validation(self, code: str):
        """
        Validate the authorization code sent to the platform.

        Args:
            code (str): The authorization code received from the platform.

        Returns:
            dict: A dictionary containing the validation result or error.
        """
        try:
            client = self.session.Methods(self.phone_number)
            result = asyncio.run(client.validate(code=code))

            return {"response": result}

        except (
            self.session.Errors.PhoneCodeInvalidError,
            self.session.Errors.PhoneCodeExpiredError,
            self.session.Errors.SessionPasswordNeededError,
            self.session.Errors.FloodWaitError,
        ) as e:
            return {"error": str(e)}

    def password_validation(self, password) -> dict:
        """
        Validate the password for two-step verification.

        Args:
            password (str): The password for two-step verification.

        Returns:
            dict: A dictionary containing the validation result or error.
        """
        try:
            client = self.session.Methods(self.phone_number)
            result = asyncio.run(client.validate_with_password(password=password))

            return {"response": result}

        except (
            self.session.Errors.PasswordHashInvalidError,
            self.session.Errors.FloodWaitError,
        ) as e:
            return {"error": str(e)}

    def invalidation(self) -> None:
        """
        Invalidate the session token.

        Returns:
            dict: A dictionary containing the invalidation result or error.
        """
        try:
            client = self.session.Methods(self.phone_number)
            asyncio.run(client.invalidate(token=self.phone_number))

            return {"response": f"Successfully revoked access for {self.platform}."}

        except Exception as error:
            logger.exception(error)
            return {"error": str(error)}
