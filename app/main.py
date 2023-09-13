"""
Functional APIs provided by the server
- /access/token
- /access/revoke
- /record/{uuid}
- /graphql/query/?mode=data/detail/facet/mri
- /graphql/pagination/?search=<string>
- /filter/?sidebar=<boolean>
- /collection
- /data/{action}/{filepath:path}
- /instance
- /dicom/export/{identifier}
"""
import io
import mimetypes
import re
import time

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi_utils.tasks import repeat_every
from gen3.auth import Gen3Auth
from gen3.submission import Gen3Submission
from irods.session import iRODSSession
from pyorthanc import Orthanc, find

from app.config import Gen3Config, OrthancConfig, iRODSConfig
from app.data_schema import *
from app.filter import Filter
from app.filter_format import FilterFormat
from app.filter_generator import FilterGenerator
from app.pagination import Pagination
from app.pagination_format import PaginationFormat
from app.query_format import QueryFormat
from app.search import Search
from app.sgqlc import SimpleGraphQLClient
from middleware.auth import Authenticator

app = FastAPI(
    title=title,
    description=description,
    contact=contact,
    openapi_tags=tags_metadata,
)

origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-File-Name"],
)

SUBMISSION = None
SESSION = None
ORTHANC = None

FF = None
FG = None
PF = None
P = None
QF = None
SGQLC = None
A = Authenticator()

FILTER_GENERATED = False


def connect_to_gen3():
    """
    Connect to gen3 server.
    """
    try:
        global SUBMISSION
        auth = Gen3Auth(
            endpoint=Gen3Config.GEN3_ENDPOINT_URL,
            refresh_token={
                "api_key": Gen3Config.GEN3_API_KEY,
                "key_id": Gen3Config.GEN3_KEY_ID,
            },
        )
        SUBMISSION = Gen3Submission(auth)
    except Exception:
        print("Encounter an error while creating the GEN3 auth.")


def connect_to_irods():
    """
    Connect to irods server.
    """
    try:
        # This function is used to connect to the iRODS server
        # It requires "host", "port", "user", "password" and "zone" environment variables.
        global SESSION
        SESSION = iRODSSession(
            host=iRODSConfig.IRODS_HOST,
            port=iRODSConfig.IRODS_PORT,
            user=iRODSConfig.IRODS_USER,
            password=iRODSConfig.IRODS_PASSWORD,
            zone=iRODSConfig.IRODS_ZONE,
        )
        # SESSION.connection_timeout =
    except Exception:
        print("Encounter an error while creating the iRODS session.")


def connect_to_orthanc():
    """
    Connect to orthanc server.
    """
    try:
        global ORTHANC
        ORTHANC = Orthanc(
            OrthancConfig.ORTHANC_ENDPOINT_URL,
            username=OrthancConfig.ORTHANC_USERNAME,
            password=OrthancConfig.ORTHANC_PASSWORD,
        )
    except Exception:
        print("Encounter an error while creating the Orthanc client.")


def check_external_service():
    """
    Check the services connection after startup and every time call apis.
    """
    service = {"gen3": False, "irods": False, "orthanc": False}
    try:
        SUBMISSION.get_programs()
        service["gen3"] = True
    except Exception:
        print("Encounter an error with gen3 submission.")

    try:
        SESSION.collections.get(iRODSConfig.IRODS_ROOT_PATH)
        service["irods"] = True
    except Exception:
        print("Encounter an error with session connection.")

    try:
        ORTHANC.get_patients()
        service["orthanc"] = True
    except Exception:
        print("Encounter an error with orthanc client.")

    if not service["gen3"] or not service["irods"] or not service["orthanc"]:
        print("Status:", service)
        if not service["gen3"]:
            connect_to_gen3()
            check_external_service()
    return service


@app.on_event("startup")
async def start_up():
    """
    Connect to services, create function objects and trigger periodic function.
    """
    connect_to_gen3()
    connect_to_irods()
    connect_to_orthanc()
    check_external_service()

    global FF, FG, PF, P, QF, SGQLC
    SGQLC = SimpleGraphQLClient(SUBMISSION)
    FG = FilterGenerator(SGQLC)
    FF = FilterFormat(FG)
    PF = PaginationFormat(FG)
    P = Pagination(FG, Filter(), Search(SESSION), SGQLC)
    QF = QueryFormat(FG)

    await periodic_execution()


@repeat_every(seconds=60 * 60 * 24)
async def periodic_execution():
    """
    Update filter and cleanup users periodically.
    """
    try:
        global FILTER_GENERATED
        FILTER_GENERATED = False
        if check_external_service()["gen3"]:
            while not FILTER_GENERATED:
                FILTER_GENERATED = FG.generate_public_filter()
                if FILTER_GENERATED:
                    print("Default filter dictionary has been updated.")
    except Exception:
        print("Failed to update the default filter dictionary")

    A.cleanup_authorized_user()


@app.get("/", tags=["Root"])
async def root():
    """
    Root
    """
    return "This is the fastapi backend."


######################
### Access Control ###
######################


@app.post(
    "/access/token",
    tags=["Access"],
    summary="Create gen3 access token for authorized user",
    responses=access_token_responses,
)
async def create_gen3_access(
    item: IdentityItem, connect_with: dict = Depends(check_external_service)
):
    """
    Return user identity and the authorized access token.

    Example identity: email@gmail.com>machine_id>expiration_time
    """
    if not connect_with["gen3"] or not connect_with["irods"]:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Please check the service (Gen3/iRODS) status",
        )
    if item.identity is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing field in the request body",
        )

    result = {
        "identity": item.identity,
        "access_token": A.generate_access_token(item.identity, SUBMISSION, SESSION),
    }
    return result


@app.delete(
    "/access/revoke",
    tags=["Access"],
    summary="Revoke gen3 access for authorized user",
    responses=access_revoke_responses,
)
async def revoke_gen3_access(is_revoked: bool = Depends(A.revoke_user_authority)):
    """
    Return revoke message if success.
    """
    if is_revoked:
        raise HTTPException(
            status_code=status.HTTP_200_OK, detail="Revoke access successfully"
        )


#########################
### Gen3 Data Commons ###
#########################


@app.get(
    "/record/{uuid}",
    tags=["Gen3"],
    summary="Get gen3 record information",
    responses=record_responses,
)
async def get_gen3_record(
    uuid: str,
    access_scope: list = Depends(A.gain_user_authority),
    connect_with: dict = Depends(check_external_service),
):
    """
    Return record information in the Gen3 Data Commons.

    - **uuid**: uuid of the record.
    """
    if not connect_with["gen3"]:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Please check the service (Gen3) status",
        )

    def handle_access(access):
        access_list = access[0].split("-")
        return access_list[0], access_list[1]

    program, project = handle_access(access_scope)
    record = SUBMISSION.export_record(program, project, uuid, "json")
    if "message" in record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{record['message']} and check if the correct project or uuid is used",
        )

    result = {"record": record[0]}
    return result


@app.post(
    "/graphql/query/",
    tags=["Gen3"],
    summary="GraphQL query gen3 metadata information",
    responses=query_responses,
)
async def get_gen3_graphql_query(
    item: GraphQLQueryItem,
    mode: ModeParam,
    access_scope: list = Depends(A.gain_user_authority),
    connect_with: dict = Depends(check_external_service),
):
    """
    Return queries metadata records. The API uses GraphQL query language.

    **node**
    - experiment_query
    - dataset_description_query
    - manifest_query
    - case_query

    **filter**
    - {"field_name": ["field_value", ...], ...}

    **search**
    - string content,
    - only available in dataset_description/manifest/case nodes
    """
    if not connect_with["gen3"]:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Please check the service (Gen3) status",
        )
    if mode not in ["data", "detail", "facet", "mri"]:
        raise HTTPException(
            status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
            detail=f"The query mode ({mode}) is not provided in this API",
        )
    # Mode detail/facet/mri only be supported when query one dataset in experiment node
    # Use to pre-process the data
    if mode != "data" and (
        "submitter_id" not in item.filter or len(item.filter["submitter_id"]) > 1
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Mode {mode} only available when query one dataset in experiment node",
        )

    QF.set_query_mode(mode)
    item.access = access_scope
    query_result = SGQLC.fetch_queried_result(item)

    def handle_result():
        if len(query_result) == 1:
            return query_result[0]
        return query_result

    return QF.process_data_output(handle_result())


@app.post(
    "/graphql/pagination/",
    tags=["Gen3"],
    summary="Display datasets",
    responses=pagination_responses,
)
async def get_gen3_graphql_pagination(
    item: GraphQLPaginationItem,
    search: str = "",
    access_scope: list = Depends(A.gain_user_authority),
    connect_with: dict = Depends(check_external_service),
):
    """
    /graphql/pagination/?search=<string>

    Return filtered/searched metadata records. The API uses GraphQL query language.

    - Default page = 1
    - Default limit = 50
    - Default filter = {}
    - Default search = ""
    - Default relation = "and"
    - Default access = gen3 public access repository
    - Default order = "published(asc)"

    **node**
    - experiment_pagination

    **filter(zero or more)**
    - {"gen3_node>gen3_field": [filter_name,...], ...}

    **search(parameter)**:
    - string content
    """
    if not connect_with["gen3"] or not connect_with["irods"]:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Please check the service (Gen3/iRODS) status",
        )

    item.access = access_scope
    is_public_access_filtered = P.update_pagination_item(item, search)
    data_count, match_pair = P.get_pagination_count(item)
    query_result = P.get_pagination_data(item, match_pair, is_public_access_filtered)
    # If both asc and desc are None, datasets ordered by self-written order function
    if item.asc is None and item.desc is None:
        query_result = sorted(
            query_result,
            key=lambda dict: item.filter["submitter_id"].index(dict["submitter_id"]),
        )
    result = {
        "items": PF.reconstruct_data_structure(query_result),
        "numberPerPage": item.limit,
        "total": data_count,
    }
    return result


@app.get(
    "/filter/",
    tags=["Gen3"],
    summary="Get filter information",
    responses=filter_responses,
)
async def get_gen3_filter(
    sidebar: bool = False,
    access_scope: list = Depends(A.gain_user_authority),
    connect_with: dict = Depends(check_external_service),
):
    """
    /filter/?sidebar=<boolean>

    Return the support data for portal filters component.

    - **sidebar**: boolean content.
    """
    if not connect_with["gen3"]:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Please check the service (Gen3) status",
        )

    retry = 0
    # Stop waiting for the filter generator after hitting the retry limits
    # The retry limit here may need to be increased if there is a large database
    # This also depends on how fast the filter will be generated
    while retry < 12 and not FILTER_GENERATED:
        retry += 1
        time.sleep(retry)
    if not FILTER_GENERATED:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Failed to generate filter or the maximum retry limit was reached",
        )

    if sidebar:
        return FF.generate_sidebar_filter_format(access_scope)
    return FF.generate_filter_format(access_scope)


############################################
### Integrated Rule-Oriented Data System ###
############################################


@app.post(
    "/collection",
    tags=["iRODS"],
    summary="Get folder information",
    responses=collection_responses,
)
async def get_irods_collection(
    item: CollectionItem, connect_with: dict = Depends(check_external_service)
):
    """
    Return all collections from the required folder.

    Root folder will be returned if no item or "/" is passed.
    """
    if not connect_with["irods"]:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Please check the service (iRODS) status",
        )
    if not re.match("(/(.)*)+", item.path):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid path format is used",
        )

    def handle_collection(data):
        collection = []
        for ele in data:
            collection.append(
                {
                    "name": ele.name,
                    "path": re.sub(iRODSConfig.IRODS_ROOT_PATH, "", ele.path),
                }
            )
        return collection

    try:
        collect = SESSION.collections.get(f"{iRODSConfig.IRODS_ROOT_PATH}{item.path}")
        folder = handle_collection(collect.subcollections)
        file = handle_collection(collect.data_objects)
        result = {"folders": folder, "files": file}
        return result
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data not found in the provided path",
        ) from error


@app.get(
    "/data/{action}/{filepath:path}",
    tags=["iRODS"],
    summary="Download irods file",
    response_description="Successfully return a file with data",
)
async def get_irods_data_file(
    action: ActionParam,
    filepath: str,
    connect_with: dict = Depends(check_external_service),
):
    """
    Used to preview most types of data files in iRODS (.xlsx and .csv not supported yet).
    OR
    Return a specific download file from iRODS or a preview of most types data.

    - **action**: Action should be either preview or download.
    - **filepath**: Required iRODS file path.
    """
    chunk_size = 1024 * 1024 * 1024

    if not connect_with["irods"]:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Please check the service (iRODS) status",
        )
    if action not in ["preview", "download"]:
        raise HTTPException(
            status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
            detail=f"The action ({action}) is not provided in this API",
        )

    try:
        file = SESSION.data_objects.get(f"{iRODSConfig.IRODS_ROOT_PATH}/{filepath}")
        filename = file.name
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data not found in the provided path",
        ) from error

    def handle_header():
        header = None
        if action == "download":
            header = {
                "X-File-Name": filename,
                "Content-Disposition": f"attachment;filename={filename}",
            }
        return header

    def handle_mimetype():
        return mimetypes.guess_type(filename)[0]

    def iterate_file():
        with file.open("r") as file_like:
            chunk = file_like.read(chunk_size)
            while chunk:
                yield chunk
                chunk = file_like.read(chunk_size)

    return StreamingResponse(
        iterate_file(), media_type=handle_mimetype(), headers=handle_header()
    )


##############################
### Orthanc - DICOM server ###
##############################


@app.post(
    "/instance",
    tags=["Orthanc"],
    summary="Get instance ids",
    responses=instance_responses,
)
async def get_orthanc_instance(
    item: InstanceItem, connect_with: dict = Depends(check_external_service)
):
    """
    Return a list of dicom instance uuids
    """
    if not connect_with["orthanc"]:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Please check the service (Orthanc) status",
        )
    if item.study is None or item.series is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing one or more fields in the request body",
        )

    patients = find(
        orthanc=ORTHANC,
        study_filter=lambda s: s.uid == item.study,
        series_filter=lambda s: s.uid == item.series,
    )
    if patients == []:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Resource is not found in the orthanc server",
        )

    result = []
    for patient in patients:
        for study in patient.studies:
            for series in study.series:
                for instance in series.instances:
                    result.append(instance.id_)
    return result


@app.get(
    "/dicom/export/{identifier}",
    tags=["Orthanc"],
    summary="Export dicom file",
    response_description="Successfully return a file with data",
)
async def get_orthanc_dicom_file(
    identifier: str, connect_with: dict = Depends(check_external_service)
):
    """
    Export a specific dicom file from Orthanc server

    - **identifier**: dicom instance uuid.
    """
    if not connect_with["orthanc"]:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Please check the service (Orthanc) status",
        )

    try:
        instance_file = ORTHANC.get_instances_id_file(identifier)
        bytes_file = io.BytesIO(instance_file)
        return Response(bytes_file.getvalue(), media_type="application/dicom")
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Resource is not found in the orthanc server",
        ) from error
