"""
Functionality for connecting and using external service
- process_irods_keyword_search
- process_gen3_graphql_query
- check_service_status
"""
import time

from fastapi import HTTPException, status
from gen3.auth import Gen3Auth, Gen3AuthError
from gen3.submission import Gen3Submission, Gen3SubmissionQueryError
from irods.column import In, Like
from irods.models import Collection, DataObjectMeta
from irods.session import iRODSSession
from pyorthanc import Orthanc

from app.config import Gen3Config, OrthancConfig, iRODSConfig

SEARCHFIELD = ["TITLE", "SUBTITLE", "CONTRIBUTOR"]


class ExternalService:
    """
    sgqlc -> simple graphql client object is required
    """

    def __init__(self, sgqlc):
        self._sgqlc = sgqlc
        self.services = {"gen3": None, "irods": None, "orthanc": None}
        self.retry = 0

    def _check_orthanc_status(self):
        """
        Handler for checking orthanc connection status
        """
        try:
            self.services["orthanc"].get_patients()
        except Exception:
            print("Orthanc disconnected.")
            self.services["orthanc"] = None

    def _connect_orthanc(self):
        """
        Handler for connecting orthanc service
        """
        try:
            self.services["orthanc"] = Orthanc(
                OrthancConfig.ORTHANC_ENDPOINT_URL,
                username=OrthancConfig.ORTHANC_USERNAME,
                password=OrthancConfig.ORTHANC_PASSWORD,
            )
            self._check_orthanc_status()
        except Exception:
            print("Failed to create the Orthanc client.")

    def process_irods_keyword_search(self, keyword):
        """
        Handler for searching keywords in irods
        """
        try:
            result = (
                self.services["irods"]
                .query(Collection.name, DataObjectMeta.value)
                .filter(In(DataObjectMeta.name, SEARCHFIELD))
                .filter(Like(DataObjectMeta.value, f"%{keyword}%"))
            )
        except Exception as error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(error)
            ) from error
        # Any keyword that does not match with the database content will cause search no result
        if len(result.all()) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="There is no matched content in the database",
            )
        return result

    def _check_irods_status(self):
        """
        Handler for checking irods connection status
        """
        try:
            self.services["irods"].collections.get(iRODSConfig.IRODS_ROOT_PATH)
        except Exception:
            print("iRODS disconnected.")
            self.services["irods"] = None

    def _connect_irods(self):
        """
        Handler for connecting irods service
        """
        try:
            # This function is used to connect to the iRODS server
            # It requires "host", "port", "user", "password" and "zone" environment variables.
            self.services["irods"] = iRODSSession(
                host=iRODSConfig.IRODS_HOST,
                port=iRODSConfig.IRODS_PORT,
                user=iRODSConfig.IRODS_USER,
                password=iRODSConfig.IRODS_PASSWORD,
                zone=iRODSConfig.IRODS_ZONE,
            )
            # self.services["irods"].connection_timeout =
            self._check_irods_status()
        except Exception:
            print("Failed to create the iRODS session.")

    def process_gen3_graphql_query(self, item, key=None, queue=None):
        """
        Handler for fetching gen3 data with graphql query code
        """
        if item.node is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing one or more fields in the request body",
            )

        query = self._sgqlc.handle_graphql_query_code(item)
        try:
            result = self.services["gen3"].query(query)["data"][item.node]
            if key is not None and queue is not None:
                queue.put({key: result})
            return result
        except Gen3SubmissionQueryError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
            ) from error

    def _check_gen3_status(self):
        """
        Handler for checking gen3 connection status
        """
        try:
            self.services["gen3"].get_programs()
            self.retry = 0
        except Gen3AuthError:
            print("Gen3 disconnected.")
            self.services["gen3"] = None
            if self.retry < 12:
                self.retry += 1
                print(f"Reconnecting...{self.retry}...")
                time.sleep(self.retry)
                self._connect_gen3()

    def _connect_gen3(self):
        """
        Handler for connecting gen3 service
        """
        try:
            self.services["gen3"] = Gen3Submission(
                Gen3Auth(
                    endpoint=Gen3Config.GEN3_ENDPOINT_URL,
                    refresh_token={
                        "api_key": Gen3Config.GEN3_API_KEY,
                        "key_id": Gen3Config.GEN3_KEY_ID,
                    },
                )
            )
            self._check_gen3_status()
        except Exception:
            print("Failed to create the Gen3 submission.")

    def check_service_status(self):
        """
        Handler for checking external services status
        """
        if self.services["gen3"] is None:
            self._connect_gen3()
        else:
            self._check_gen3_status()

        if self.services["irods"] is None:
            self._connect_irods()
        else:
            self._check_irods_status()

        if self.services["orthanc"] is None:
            self._connect_orthanc()
        else:
            self._check_orthanc_status()
        return self.services
