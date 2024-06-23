"""gRPC Publisher Service"""

import logging
import base64
import json
import grpc

from authlib.integrations.base_client import OAuthError

import publisher_pb2
import publisher_pb2_grpc

from utils import (
    error_response,
    validate_request_fields,
    create_email_message,
    parse_email_content,
)
from oauth2 import OAuth2Client
from relaysms_payload import decode_relay_sms_payload
from grpc_vault_entity_client import (
    list_entity_stored_tokens,
    store_entity_token,
    get_entity_access_token,
    decrypt_payload,
    encrypt_payload,
    update_entity_token,
    delete_entity_token,
)

SUPPORTED_PLATFORMS = {
    "gmail": {
        "shortcode": "g",
        "service_type": "email",
        "protocol": "oauth2",
    }
}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("[gRPC Publisher Service]")


def create_update_token_context(
    device_id, account_identifier, platform_name, response, context
):
    """
    Creates a context-specific token update function.

    Args:
        device_id (str): The unique identifier of the device.
        account_identifier (str): The identifier for the account
            (e.g., email or username).
        platform_name (str): The name of the platform (e.g., 'gmail').
        response (protobuf message class): The response class for the gRPC method.
        context (grpc.ServicerContext): The gRPC context for the current method call.

    Returns:
        function: A function `update_token(token)` that updates the token information.
    """

    def update_token(token, **kwargs):
        """
        Updates the stored token for a specific entity.

        Args:
            token (dict or object): The token information
                containing access and refresh tokens.
        """
        logger.info(
            "Updating token for device_id: %s, platform: %s",
            device_id,
            platform_name,
        )

        update_entity_token_response, update_entity_token_error = update_entity_token(
            device_id=device_id,
            token=json.dumps(token),
            account_identifier=account_identifier,
            platform=platform_name,
        )

        if update_entity_token_error:
            return error_response(
                context,
                response,
                update_entity_token_error.details(),
                update_entity_token_error.code(),
            )

        if not update_entity_token_response.success:
            return response(
                message=update_entity_token_response.message,
                success=update_entity_token_response.success,
            )

        return True

    return update_token


def check_platform_supported(platform_name, protocol):
    """
    Check if the given platform is supported for the specified protocol.

    Args:
        platform_name (str): The platform name to check.
        protocol (str): The protocol to check for the given platform.

    Raises:
        NotImplementedError: If the platform is not supported or the protocol
            does not match the supported protocol.
    """
    platform_details = SUPPORTED_PLATFORMS.get(platform_name)

    if not platform_details:
        raise NotImplementedError(
            f"The platform '{platform_name}' is currently not supported. "
            "Please contact the developers for more information on when "
            "this platform will be implemented."
        )

    expected_protocol = platform_details.get("protocol")

    if protocol != expected_protocol:
        raise NotImplementedError(
            f"The protocol '{protocol}' for platform '{platform_name}' "
            "is currently not supported. "
            f"Expected protocol: '{expected_protocol}'."
        )


def get_platform_details_by_shortcode(shortcode):
    """
    Get the platform details corresponding to the given shortcode.

    Args:
        shortcode (str): The shortcode to look up.

    Returns:
        tuple: A tuple containing (platform_details, error_message).
            - platform_details (dict): Details of the platform if found.
            - error_message (str): Error message if platform is not found,
    """
    for platform_name, details in SUPPORTED_PLATFORMS.items():
        if details.get("shortcode") == shortcode:
            details["name"] = platform_name
            return details, None

    available_platforms = ", ".join(
        f"'{details['shortcode']}' for {platform_name}"
        for platform_name, details in SUPPORTED_PLATFORMS.items()
    )
    error_message = (
        f"No platform found for shortcode '{shortcode}'. "
        f"Available shortcodes: {available_platforms}"
    )

    return None, error_message


class PublisherService(publisher_pb2_grpc.PublisherServicer):
    """Publisher Service Descriptor"""

    def GetOAuth2AuthorizationUrl(self, request, context):
        """Handles generating OAuth2 authorization URL"""

        response = publisher_pb2.GetOAuth2AuthorizationUrlResponse

        def validate_fields():
            return validate_request_fields(
                context,
                request,
                response,
                ["platform"],
            )

        try:
            invalid_fields_response = validate_fields()
            if invalid_fields_response:
                return invalid_fields_response

            check_platform_supported(request.platform.lower(), "oauth2")

            oauth2_client = OAuth2Client(request.platform)

            extra_params = {
                "state": getattr(request, "state") or None,
                "code_verifier": getattr(request, "code_verifier") or None,
                "autogenerate_code_verifier": getattr(
                    request, "autogenerate_code_verifier"
                ),
            }

            authorization_url, state, code_verifier = (
                oauth2_client.get_authorization_url(**extra_params)
            )

            return response(
                authorization_url=authorization_url,
                state=state,
                code_verifier=code_verifier,
                message="Successfully generated authorization url",
            )

        except NotImplementedError as e:
            return error_response(
                context,
                response,
                str(e),
                grpc.StatusCode.UNIMPLEMENTED,
            )

        except Exception as exc:
            return error_response(
                context,
                response,
                exc,
                grpc.StatusCode.INTERNAL,
                user_msg="Oops! Something went wrong. Please try again later.",
                _type="UNKNOWN",
            )

    def ExchangeOAuth2CodeAndStore(self, request, context):
        """Handles exchanging OAuth2 authorization code for a token"""

        response = publisher_pb2.ExchangeOAuth2CodeAndStoreResponse

        def validate_fields():
            return validate_request_fields(
                context,
                request,
                response,
                ["long_lived_token", "platform", "authorization_code"],
            )

        def list_tokens():
            list_response, list_error = list_entity_stored_tokens(
                long_lived_token=request.long_lived_token
            )
            if list_error:
                return None, error_response(
                    context,
                    response,
                    list_error.details(),
                    list_error.code(),
                    _type="UNKNOWN",
                )
            return list_response, None

        def fetch_token_and_profile():
            oauth2_client = OAuth2Client(request.platform)
            extra_params = {"code_verifier": getattr(request, "code_verifier") or None}
            token = oauth2_client.fetch_token(
                code=request.authorization_code, **extra_params
            )
            profile = oauth2_client.fetch_userinfo()
            return token, profile

        def store_token(token, profile):
            store_response, store_error = store_entity_token(
                long_lived_token=request.long_lived_token,
                platform=request.platform,
                account_identifier=profile.get("email") or profile.get("username"),
                token=json.dumps(token),
            )

            if store_error:
                return error_response(
                    context,
                    response,
                    store_error.details(),
                    store_error.code(),
                    _type="UNKNOWN",
                )

            if not store_response.success:
                return response(
                    message=store_response.message, success=store_response.success
                )

            return response(
                success=True, message="Successfully fetched and stored token"
            )

        try:
            invalid_fields_response = validate_fields()
            if invalid_fields_response:
                return invalid_fields_response

            check_platform_supported(request.platform.lower(), "oauth2")

            _, token_list_error = list_tokens()
            if token_list_error:
                return token_list_error

            token, profile = fetch_token_and_profile()
            return store_token(token, profile)

        except OAuthError as e:
            return error_response(
                context,
                response,
                str(e),
                grpc.StatusCode.INVALID_ARGUMENT,
                _type="UNKNOWN",
            )

        except NotImplementedError as e:
            return error_response(
                context,
                response,
                str(e),
                grpc.StatusCode.UNIMPLEMENTED,
            )

        except Exception as exc:
            return error_response(
                context,
                response,
                exc,
                grpc.StatusCode.INTERNAL,
                user_msg="Oops! Something went wrong. Please try again later.",
                _type="UNKNOWN",
            )

    def RevokeAndDeleteOAuth2Token(self, request, context):
        """Handles revoking and deleting OAuth2 access tokens"""

        response = publisher_pb2.RevokeAndDeleteOAuth2TokenResponse

        def validate_fields():
            return validate_request_fields(
                context,
                request,
                response,
                ["long_lived_token", "platform", "account_identifier"],
            )

        def get_access_token():
            get_access_token_response, get_access_token_error = get_entity_access_token(
                platform=request.platform,
                account_identifier=request.account_identifier,
                long_lived_token=request.long_lived_token,
            )
            if get_access_token_error:
                return None, error_response(
                    context,
                    response,
                    get_access_token_error.details(),
                    get_access_token_error.code(),
                )
            if not get_access_token_response.success:
                return None, response(
                    message=get_access_token_response.message,
                    success=get_access_token_response.success,
                )
            return get_access_token_response.token, None

        def revoke_token(token):
            oauth2_client = OAuth2Client(request.platform, json.loads(token))
            revoke_response = oauth2_client.revoke_token()
            return revoke_response

        def delete_token():
            delete_token_response, delete_token_error = delete_entity_token(
                request.long_lived_token, request.platform, request.account_identifier
            )

            if delete_token_error:
                return error_response(
                    context,
                    response,
                    delete_token_error.details(),
                    delete_token_error.code(),
                )

            if not delete_token_response.success:
                return response(
                    message=delete_token_response.message,
                    success=delete_token_response.success,
                )

            return response(success=True, message="Successfully deleted token")

        try:
            invalid_fields_response = validate_fields()
            if invalid_fields_response:
                return invalid_fields_response

            check_platform_supported(request.platform.lower(), "oauth2")

            access_token, access_token_error = get_access_token()
            if access_token_error:
                return access_token_error

            revoke_token(access_token)
            return delete_token()

        except NotImplementedError as e:
            return error_response(
                context,
                response,
                str(e),
                grpc.StatusCode.UNIMPLEMENTED,
            )

        except Exception as exc:
            return error_response(
                context,
                response,
                exc,
                grpc.StatusCode.INTERNAL,
                user_msg="Oops! Something went wrong. Please try again later.",
                _type="UNKNOWN",
            )

    def PublishContent(self, request, context):
        """Handles publishing relaysms payload"""

        response = publisher_pb2.PublishContentResponse

        def validate_fields():
            return validate_request_fields(context, request, response, ["content"])

        def decode_payload():
            platform_letter, encrypted_content, device_id, decode_error = (
                decode_relay_sms_payload(request.content)
            )
            if decode_error:
                return None, error_response(
                    context,
                    response,
                    decode_error,
                    grpc.StatusCode.INVALID_ARGUMENT,
                    user_msg="Invalid content format.",
                    _type="UNKNOWN",
                )
            return (platform_letter, encrypted_content, device_id), None

        def get_platform_info(platform_letter):
            platform_info, platform_err = get_platform_details_by_shortcode(
                platform_letter
            )
            if platform_info is None:
                return None, error_response(
                    context,
                    response,
                    platform_err,
                    grpc.StatusCode.INVALID_ARGUMENT,
                )
            return platform_info, None

        def get_access_token(device_id, platform_name, account_identifier):
            get_access_token_response, get_access_token_error = get_entity_access_token(
                device_id=device_id.hex(),
                platform=platform_name,
                account_identifier=account_identifier,
            )
            if get_access_token_error:
                return None, error_response(
                    context,
                    response,
                    get_access_token_error.details(),
                    get_access_token_error.code(),
                )
            if not get_access_token_response.success:
                return None, response(
                    message=get_access_token_response.message,
                    success=get_access_token_response.success,
                )
            return get_access_token_response.token, None

        def decrypt_message(device_id, encrypted_content):
            decrypt_payload_response, decrypt_payload_error = decrypt_payload(
                device_id.hex(), base64.b64encode(encrypted_content).decode("utf-8")
            )
            if decrypt_payload_error:
                return None, error_response(
                    context,
                    response,
                    decrypt_payload_error.details(),
                    decrypt_payload_error.code(),
                )
            if not decrypt_payload_response.success:
                return None, response(
                    message=decrypt_payload_response.message,
                    success=decrypt_payload_response.success,
                )
            return decrypt_payload_response.payload_plaintext, None

        def encrypt_message(device_id, plaintext):
            encrypt_payload_response, encrypt_payload_error = encrypt_payload(
                device_id, plaintext
            )
            if encrypt_payload_error:
                return None, error_response(
                    context,
                    response,
                    encrypt_payload_error.details(),
                    encrypt_payload_error.code(),
                )
            if not encrypt_payload_response.success:
                return None, response(
                    message=encrypt_payload_response.message,
                    success=encrypt_payload_response.success,
                )
            return encrypt_payload_response.payload_ciphertext, None

        def handle_oauth2_email(device_id, platform_name, payload, token):
            from_email, to_email, cc_email, bcc_email, subject, body = (
                parse_email_content(payload)
            )
            email_message = create_email_message(
                from_email,
                to_email,
                subject,
                body,
                cc_email=cc_email,
                bcc_email=bcc_email,
            )
            oauth2_client = OAuth2Client(
                platform_name,
                json.loads(token),
                create_update_token_context(
                    device_id, from_email, platform_name, response, context
                ),
            )
            return oauth2_client.send_message(from_email, email_message)

        try:
            invalid_fields_response = validate_fields()
            if invalid_fields_response:
                return invalid_fields_response

            decoded_payload, decoding_error = decode_payload()
            if decoding_error:
                return decoding_error

            platform_letter, encrypted_content, device_id = decoded_payload

            platform_info, platform_info_error = get_platform_info(platform_letter)
            if platform_info_error:
                return platform_info_error

            decrypted_content, decrypt_error = decrypt_message(
                device_id, encrypted_content
            )

            if decrypt_error:
                return decrypt_error

            access_token, access_token_error = get_access_token(
                device_id, platform_info["name"], decrypted_content.split(":")[0]
            )
            if access_token_error:
                return access_token_error

            message_response = None
            if (
                platform_info["protocol"] == "oauth2"
                and platform_info["service_type"] == "email"
            ):
                message_response = handle_oauth2_email(
                    device_id.hex(),
                    platform_info["name"],
                    decrypted_content,
                    access_token,
                )

            # payload_ciphertext, encrypt_payload_error = encrypt_message(
            #     device_id, message_response
            # )
            # if encrypt_payload_error:
            #     return encrypt_payload_error

            return response(
                message=f"Successfully published {platform_info['name']} message",
                publisher_response=message_response,
                success=True,
            )

        except Exception as exc:
            return error_response(
                context,
                response,
                exc,
                grpc.StatusCode.INTERNAL,
                user_msg="Oops! Something went wrong. Please try again later.",
                _type="UNKNOWN",
            )