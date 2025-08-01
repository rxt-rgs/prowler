import json
import time
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone

from celery.utils.log import get_task_logger
from config.settings.celery import CELERY_DEADLOCK_ATTEMPTS
from django.db import IntegrityError, OperationalError
from django.db.models import Case, Count, IntegerField, Prefetch, Sum, When
from tasks.utils import CustomEncoder

from api.compliance import (
    PROWLER_COMPLIANCE_OVERVIEW_TEMPLATE,
    generate_scan_compliance,
)
from api.db_utils import (
    create_objects_in_batches,
    rls_transaction,
    update_objects_in_batches,
)
from api.exceptions import ProviderConnectionError
from api.models import (
    ComplianceRequirementOverview,
    Finding,
    Processor,
    Provider,
    Resource,
    ResourceScanSummary,
    ResourceTag,
    Scan,
    ScanSummary,
    StateChoices,
)
from api.models import StatusChoices as FindingStatus
from api.utils import initialize_prowler_provider, return_prowler_provider
from api.v1.serializers import ScanTaskSerializer
from prowler.lib.outputs.finding import Finding as ProwlerFinding
from prowler.lib.scan.scan import Scan as ProwlerScan

logger = get_task_logger(__name__)


def _create_finding_delta(
    last_status: FindingStatus | None | str, new_status: FindingStatus | None
) -> Finding.DeltaChoices:
    """
    Determine the delta status of a finding based on its previous and current status.

    Args:
        last_status (FindingStatus | None | str): The previous status of the finding. Can be None or a string representation.
        new_status (FindingStatus | None): The current status of the finding.

    Returns:
        Finding.DeltaChoices: The delta status indicating if the finding is new, changed, or unchanged.
            - Returns `Finding.DeltaChoices.NEW` if `last_status` is None.
            - Returns `Finding.DeltaChoices.CHANGED` if `last_status` and `new_status` are different.
            - Returns `None` if the status hasn't changed.
    """
    if last_status is None:
        return Finding.DeltaChoices.NEW
    return Finding.DeltaChoices.CHANGED if last_status != new_status else None


def _store_resources(
    finding: ProwlerFinding, tenant_id: str, provider_instance: Provider
) -> tuple[Resource, tuple[str, str]]:
    """
    Store resource information from a finding, including tags, in the database.

    Args:
        finding (ProwlerFinding): The finding object containing resource information.
        tenant_id (str): The ID of the tenant owning the resource.
        provider_instance (Provider): The provider instance associated with the resource.

    Returns:
        tuple:
            - Resource: The resource instance created or retrieved from the database.
            - tuple[str, str]: A tuple containing the resource UID and region.

    """
    with rls_transaction(tenant_id):
        resource_instance, created = Resource.objects.get_or_create(
            tenant_id=tenant_id,
            provider=provider_instance,
            uid=finding.resource_uid,
            defaults={
                "region": finding.region,
                "service": finding.service_name,
                "type": finding.resource_type,
            },
        )

        if not created:
            resource_instance.region = finding.region
            resource_instance.service = finding.service_name
            resource_instance.type = finding.resource_type
            resource_instance.save()
    with rls_transaction(tenant_id):
        tags = [
            ResourceTag.objects.get_or_create(
                tenant_id=tenant_id, key=key, value=value
            )[0]
            for key, value in finding.resource_tags.items()
        ]
        resource_instance.upsert_or_delete_tags(tags=tags)
    return resource_instance, (resource_instance.uid, resource_instance.region)


def perform_prowler_scan(
    tenant_id: str,
    scan_id: str,
    provider_id: str,
    checks_to_execute: list[str] | None = None,
):
    """
    Perform a scan using Prowler and store the findings and resources in the database.

    Args:
        tenant_id (str): The ID of the tenant for which the scan is performed.
        scan_id (str): The ID of the scan instance.
        provider_id (str): The ID of the provider to scan.
        checks_to_execute (list[str], optional): A list of specific checks to execute. Defaults to None.

    Returns:
        dict: Serialized data of the completed scan instance.

    Raises:
        ValueError: If the provider cannot be connected.

    """
    exception = None
    unique_resources = set()
    scan_resource_cache: set[tuple[str, str, str, str]] = set()
    start_time = time.time()
    exc = None

    with rls_transaction(tenant_id):
        provider_instance = Provider.objects.get(pk=provider_id)
        scan_instance = Scan.objects.get(pk=scan_id)
        scan_instance.state = StateChoices.EXECUTING
        scan_instance.started_at = datetime.now(tz=timezone.utc)
        scan_instance.save()

    # Find the mutelist processor if it exists
    with rls_transaction(tenant_id):
        try:
            mutelist_processor = Processor.objects.get(
                tenant_id=tenant_id, processor_type=Processor.ProcessorChoices.MUTELIST
            )
        except Processor.DoesNotExist:
            mutelist_processor = None
        except Exception as e:
            logger.error(f"Error processing mutelist rules: {e}")
            mutelist_processor = None

    try:
        with rls_transaction(tenant_id):
            try:
                prowler_provider = initialize_prowler_provider(
                    provider_instance, mutelist_processor
                )
                provider_instance.connected = True
            except Exception as e:
                provider_instance.connected = False
                exc = ProviderConnectionError(
                    f"Provider {provider_instance.provider} is not connected: {e}"
                )
            finally:
                provider_instance.connection_last_checked_at = datetime.now(
                    tz=timezone.utc
                )
                provider_instance.save()

        # If the provider is not connected, raise an exception outside the transaction.
        # If raised within the transaction, the transaction will be rolled back and the provider will not be marked
        # as not connected.
        if exc:
            raise exc

        prowler_scan = ProwlerScan(provider=prowler_provider, checks=checks_to_execute)

        resource_cache = {}
        tag_cache = {}
        last_status_cache = {}
        resource_failed_findings_cache = defaultdict(int)

        for progress, findings in prowler_scan.scan():
            for finding in findings:
                if finding is None:
                    logger.error(f"None finding detected on scan {scan_id}.")
                    continue
                for attempt in range(CELERY_DEADLOCK_ATTEMPTS):
                    try:
                        with rls_transaction(tenant_id):
                            # Process resource
                            resource_uid = finding.resource_uid
                            if resource_uid not in resource_cache:
                                # Get or create the resource
                                resource_instance, _ = Resource.objects.get_or_create(
                                    tenant_id=tenant_id,
                                    provider=provider_instance,
                                    uid=resource_uid,
                                    defaults={
                                        "region": finding.region,
                                        "service": finding.service_name,
                                        "type": finding.resource_type,
                                        "name": finding.resource_name,
                                    },
                                )
                                resource_cache[resource_uid] = resource_instance

                                # Initialize all processed resources in the cache
                                resource_failed_findings_cache[resource_uid] = 0
                            else:
                                resource_instance = resource_cache[resource_uid]

                        # Update resource fields if necessary
                        updated_fields = []
                        if (
                            finding.region
                            and resource_instance.region != finding.region
                        ):
                            resource_instance.region = finding.region
                            updated_fields.append("region")
                        if resource_instance.service != finding.service_name:
                            resource_instance.service = finding.service_name
                            updated_fields.append("service")
                        if resource_instance.type != finding.resource_type:
                            resource_instance.type = finding.resource_type
                            updated_fields.append("type")
                        if resource_instance.metadata != finding.resource_metadata:
                            resource_instance.metadata = json.dumps(
                                finding.resource_metadata, cls=CustomEncoder
                            )
                            updated_fields.append("metadata")
                        if resource_instance.details != finding.resource_details:
                            resource_instance.details = finding.resource_details
                            updated_fields.append("details")
                        if resource_instance.partition != finding.partition:
                            resource_instance.partition = finding.partition
                            updated_fields.append("partition")
                        if updated_fields:
                            with rls_transaction(tenant_id):
                                resource_instance.save(update_fields=updated_fields)
                    except (OperationalError, IntegrityError) as db_err:
                        if attempt < CELERY_DEADLOCK_ATTEMPTS - 1:
                            logger.warning(
                                f"{'Deadlock error' if isinstance(db_err, OperationalError) else 'Integrity error'} "
                                f"detected when processing resource {resource_uid} on scan {scan_id}. Retrying..."
                            )
                            time.sleep(0.1 * (2**attempt))
                            continue
                        else:
                            raise db_err

                # Update tags
                tags = []
                with rls_transaction(tenant_id):
                    for key, value in finding.resource_tags.items():
                        tag_key = (key, value)
                        if tag_key not in tag_cache:
                            tag_instance, _ = ResourceTag.objects.get_or_create(
                                tenant_id=tenant_id, key=key, value=value
                            )
                            tag_cache[tag_key] = tag_instance
                        else:
                            tag_instance = tag_cache[tag_key]
                        tags.append(tag_instance)
                    resource_instance.upsert_or_delete_tags(tags=tags)

                unique_resources.add((resource_instance.uid, resource_instance.region))

                # Process finding
                with rls_transaction(tenant_id):
                    finding_uid = finding.uid
                    last_first_seen_at = None
                    if finding_uid not in last_status_cache:
                        most_recent_finding = (
                            Finding.all_objects.filter(
                                tenant_id=tenant_id, uid=finding_uid
                            )
                            .order_by("-inserted_at")
                            .values("status", "first_seen_at")
                            .first()
                        )
                        last_status = None
                        if most_recent_finding:
                            last_status = most_recent_finding["status"]
                            last_first_seen_at = most_recent_finding["first_seen_at"]
                        last_status_cache[finding_uid] = last_status, last_first_seen_at
                    else:
                        last_status, last_first_seen_at = last_status_cache[finding_uid]

                    status = FindingStatus[finding.status]
                    delta = _create_finding_delta(last_status, status)
                    # For the findings prior to the change, when a first finding is found with delta!="new" it will be
                    # assigned a current date as first_seen_at and the successive findings with the same UID will
                    # always get the date of the previous finding.
                    # For new findings, when a finding (delta="new") is found for the first time, the first_seen_at
                    # attribute will be assigned the current date, the following findings will get that date.
                    if not last_first_seen_at:
                        last_first_seen_at = datetime.now(tz=timezone.utc)

                    # If the finding is muted at this time the reason must be the configured Mutelist
                    muted_reason = "Muted by mutelist" if finding.muted else None

                    # Create the finding
                    finding_instance = Finding.objects.create(
                        tenant_id=tenant_id,
                        uid=finding_uid,
                        delta=delta,
                        check_metadata=finding.get_metadata(),
                        status=status,
                        status_extended=finding.status_extended,
                        severity=finding.severity,
                        impact=finding.severity,
                        raw_result=finding.raw,
                        check_id=finding.check_id,
                        scan=scan_instance,
                        first_seen_at=last_first_seen_at,
                        muted=finding.muted,
                        muted_reason=muted_reason,
                        compliance=finding.compliance,
                    )
                    finding_instance.add_resources([resource_instance])

                    # Increment failed_findings_count cache if the finding status is FAIL and not muted
                    if status == FindingStatus.FAIL and not finding.muted:
                        resource_uid = finding.resource_uid
                        resource_failed_findings_cache[resource_uid] += 1

                # Update scan resource summaries
                scan_resource_cache.add(
                    (
                        str(resource_instance.id),
                        resource_instance.service,
                        resource_instance.region,
                        resource_instance.type,
                    )
                )

            # Update scan progress
            with rls_transaction(tenant_id):
                scan_instance.progress = progress
                scan_instance.save()

        scan_instance.state = StateChoices.COMPLETED

        # Update failed_findings_count for all resources in batches if scan completed successfully
        if resource_failed_findings_cache:
            resources_to_update = []
            for resource_uid, failed_count in resource_failed_findings_cache.items():
                if resource_uid in resource_cache:
                    resource_instance = resource_cache[resource_uid]
                    resource_instance.failed_findings_count = failed_count
                    resources_to_update.append(resource_instance)

            if resources_to_update:
                update_objects_in_batches(
                    tenant_id=tenant_id,
                    model=Resource,
                    objects=resources_to_update,
                    fields=["failed_findings_count"],
                    batch_size=1000,
                )

    except Exception as e:
        logger.error(f"Error performing scan {scan_id}: {e}")
        exception = e
        scan_instance.state = StateChoices.FAILED

    finally:
        with rls_transaction(tenant_id):
            scan_instance.duration = time.time() - start_time
            scan_instance.completed_at = datetime.now(tz=timezone.utc)
            scan_instance.unique_resource_count = len(unique_resources)
            scan_instance.save()

    if exception is not None:
        raise exception

    try:
        resource_scan_summaries = [
            ResourceScanSummary(
                tenant_id=tenant_id,
                scan_id=scan_id,
                resource_id=resource_id,
                service=service,
                region=region,
                resource_type=resource_type,
            )
            for resource_id, service, region, resource_type in scan_resource_cache
        ]
        with rls_transaction(tenant_id):
            ResourceScanSummary.objects.bulk_create(
                resource_scan_summaries, batch_size=500, ignore_conflicts=True
            )
    except Exception as filter_exception:
        import sentry_sdk

        sentry_sdk.capture_exception(filter_exception)
        logger.error(
            f"Error storing filter values for scan {scan_id}: {filter_exception}"
        )

    serializer = ScanTaskSerializer(instance=scan_instance)
    return serializer.data


def aggregate_findings(tenant_id: str, scan_id: str):
    """
    Aggregates findings for a given scan and stores the results in the ScanSummary table.

    This function retrieves all findings associated with a given `scan_id` and calculates various
    metrics such as counts of failed, passed, and muted findings, as well as their deltas (new,
    changed, unchanged). The results are grouped by `check_id`, `service`, `severity`, and `region`.
    These aggregated metrics are then stored in the `ScanSummary` table.

    Additionally, it updates the failed_findings_count field for each resource based on the most
    recent findings for each finding.uid.

    Args:
        tenant_id (str): The ID of the tenant to which the scan belongs.
        scan_id (str): The ID of the scan for which findings need to be aggregated.

    Aggregated Metrics:
        - fail: Total number of failed findings.
        - _pass: Total number of passed findings.
        - muted: Total number of muted findings.
        - total: Total number of findings.
        - new: Total number of new findings.
        - changed: Total number of changed findings.
        - unchanged: Total number of unchanged findings.
        - fail_new: Failed findings with a delta of 'new'.
        - fail_changed: Failed findings with a delta of 'changed'.
        - pass_new: Passed findings with a delta of 'new'.
        - pass_changed: Passed findings with a delta of 'changed'.
        - muted_new: Muted findings with a delta of 'new'.
        - muted_changed: Muted findings with a delta of 'changed'.
    """
    with rls_transaction(tenant_id):
        findings = Finding.objects.filter(tenant_id=tenant_id, scan_id=scan_id)

        aggregation = findings.values(
            "check_id",
            "resources__service",
            "severity",
            "resources__region",
        ).annotate(
            fail=Sum(
                Case(
                    When(status="FAIL", muted=False, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
            _pass=Sum(
                Case(
                    When(status="PASS", muted=False, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
            muted_count=Sum(
                Case(
                    When(muted=True, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
            total=Count("id"),
            new=Sum(
                Case(
                    When(delta="new", muted=False, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
            changed=Sum(
                Case(
                    When(delta="changed", muted=False, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
            unchanged=Sum(
                Case(
                    When(delta__isnull=True, muted=False, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
            fail_new=Sum(
                Case(
                    When(delta="new", status="FAIL", muted=False, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
            fail_changed=Sum(
                Case(
                    When(delta="changed", status="FAIL", muted=False, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
            pass_new=Sum(
                Case(
                    When(delta="new", status="PASS", muted=False, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
            pass_changed=Sum(
                Case(
                    When(delta="changed", status="PASS", muted=False, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
            muted_new=Sum(
                Case(
                    When(delta="new", muted=True, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
            muted_changed=Sum(
                Case(
                    When(delta="changed", muted=True, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
        )

    with rls_transaction(tenant_id):
        scan_aggregations = {
            ScanSummary(
                tenant_id=tenant_id,
                scan_id=scan_id,
                check_id=agg["check_id"],
                service=agg["resources__service"],
                severity=agg["severity"],
                region=agg["resources__region"],
                fail=agg["fail"],
                _pass=agg["_pass"],
                muted=agg["muted_count"],
                total=agg["total"],
                new=agg["new"],
                changed=agg["changed"],
                unchanged=agg["unchanged"],
                fail_new=agg["fail_new"],
                fail_changed=agg["fail_changed"],
                pass_new=agg["pass_new"],
                pass_changed=agg["pass_changed"],
                muted_new=agg["muted_new"],
                muted_changed=agg["muted_changed"],
            )
            for agg in aggregation
        }
        ScanSummary.objects.bulk_create(scan_aggregations, batch_size=3000)


def create_compliance_requirements(tenant_id: str, scan_id: str):
    """
    Create detailed compliance requirement overview records for a scan.

    This function processes the compliance data collected during a scan and creates
    individual records for each compliance requirement in each region. These detailed
    records provide a granular view of compliance status.

    Args:
        tenant_id (str): The ID of the tenant for which to create records.
        scan_id (str): The ID of the scan for which to create records.

    Returns:
        dict: A dictionary containing the number of requirements created and the regions processed.

    Raises:
        ValidationError: If tenant_id is not a valid UUID.
    """
    try:
        with rls_transaction(tenant_id):
            scan_instance = Scan.objects.get(pk=scan_id)
            provider_instance = scan_instance.provider
            prowler_provider = return_prowler_provider(provider_instance)

        # Get check status data by region from findings
        findings = (
            Finding.all_objects.filter(scan_id=scan_id, muted=False)
            .only("id", "check_id", "status")
            .prefetch_related(
                Prefetch(
                    "resources",
                    queryset=Resource.objects.only("id", "region"),
                    to_attr="small_resources",
                )
            )
            .iterator(chunk_size=1000)
        )

        check_status_by_region = {}
        with rls_transaction(tenant_id):
            for finding in findings:
                for resource in finding.small_resources:
                    region = resource.region
                    current_status = check_status_by_region.setdefault(region, {})
                    if current_status.get(finding.check_id) != "FAIL":
                        current_status[finding.check_id] = finding.status

        try:
            # Try to get regions from provider
            regions = prowler_provider.get_regions()
        except (AttributeError, Exception):
            # If not available, use regions from findings
            regions = set(check_status_by_region.keys())

        # Get compliance template for the provider
        compliance_template = PROWLER_COMPLIANCE_OVERVIEW_TEMPLATE[
            provider_instance.provider
        ]

        # Create compliance data by region
        compliance_overview_by_region = {
            region: deepcopy(compliance_template) for region in regions
        }

        # Apply check statuses to compliance data
        for region, check_status in check_status_by_region.items():
            compliance_data = compliance_overview_by_region.setdefault(
                region, deepcopy(compliance_template)
            )
            for check_name, status in check_status.items():
                generate_scan_compliance(
                    compliance_data,
                    provider_instance.provider,
                    check_name,
                    status,
                )

        # Prepare compliance requirement objects
        compliance_requirement_objects = []
        for region, compliance_data in compliance_overview_by_region.items():
            for compliance_id, compliance in compliance_data.items():
                # Create an overview record for each requirement within each compliance framework
                for requirement_id, requirement in compliance["requirements"].items():
                    compliance_requirement_objects.append(
                        ComplianceRequirementOverview(
                            tenant_id=tenant_id,
                            scan=scan_instance,
                            region=region,
                            compliance_id=compliance_id,
                            framework=compliance["framework"],
                            version=compliance["version"],
                            requirement_id=requirement_id,
                            description=requirement["description"],
                            passed_checks=requirement["checks_status"]["pass"],
                            failed_checks=requirement["checks_status"]["fail"],
                            total_checks=requirement["checks_status"]["total"],
                            requirement_status=requirement["status"],
                        )
                    )

        # Bulk create requirement records
        create_objects_in_batches(
            tenant_id, ComplianceRequirementOverview, compliance_requirement_objects
        )

        return {
            "requirements_created": len(compliance_requirement_objects),
            "regions_processed": list(regions),
            "compliance_frameworks": (
                list(compliance_overview_by_region.get(list(regions)[0], {}).keys())
                if regions
                else []
            ),
        }

    except Exception as e:
        logger.error(f"Error creating compliance requirements for scan {scan_id}: {e}")
        raise e
