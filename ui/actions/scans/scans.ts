"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

import {
  apiBaseUrl,
  getAuthHeaders,
  getErrorMessage,
  parseStringify,
} from "@/lib";

export const getScans = async ({
  page = 1,
  query = "",
  sort = "",
  filters = {},
  pageSize = 10,
  fields = {},
  include = "",
}) => {
  const headers = await getAuthHeaders({ contentType: false });

  if (isNaN(Number(page)) || page < 1) redirect("/scans");

  const url = new URL(`${apiBaseUrl}/scans`);

  if (page) url.searchParams.append("page[number]", page.toString());
  if (pageSize) url.searchParams.append("page[size]", pageSize.toString());
  if (query) url.searchParams.append("filter[search]", query);
  if (sort) url.searchParams.append("sort", sort);
  if (include) url.searchParams.append("include", include);

  // Handle fields parameters
  Object.entries(fields).forEach(([key, value]) => {
    url.searchParams.append(`fields[${key}]`, String(value));
  });

  // Add dynamic filters (e.g., "filter[state]", "fields[scans]")
  Object.entries(filters).forEach(([key, value]) => {
    url.searchParams.append(key, String(value));
  });

  try {
    const response = await fetch(url.toString(), { headers });
    const data = await response.json();
    const parsedData = parseStringify(data);
    revalidatePath("/scans");
    return parsedData;
  } catch (error) {
    // eslint-disable-next-line no-console
    console.error("Error fetching scans:", error);
    return undefined;
  }
};

export const getScansByState = async () => {
  const headers = await getAuthHeaders({ contentType: false });

  const url = new URL(`${apiBaseUrl}/scans`);

  // Request only the necessary fields to optimize the response
  url.searchParams.append("fields[scans]", "state");

  try {
    const response = await fetch(url.toString(), {
      headers,
    });

    if (!response.ok) {
      try {
        const errorData = await response.json();
        throw new Error(errorData?.message || "Failed to fetch scans by state");
      } catch {
        throw new Error("Failed to fetch scans by state");
      }
    }

    const data = await response.json();

    return parseStringify(data);
  } catch (error) {
    // eslint-disable-next-line no-console
    console.error("Error fetching scans by state:", error);
    return undefined;
  }
};

export const getScan = async (scanId: string) => {
  const headers = await getAuthHeaders({ contentType: false });

  const url = new URL(`${apiBaseUrl}/scans/${scanId}`);

  try {
    const scan = await fetch(url.toString(), {
      headers,
    });
    const data = await scan.json();
    const parsedData = parseStringify(data);

    return parsedData;
  } catch (error) {
    return {
      error: getErrorMessage(error),
    };
  }
};

export const scanOnDemand = async (formData: FormData) => {
  const headers = await getAuthHeaders({ contentType: true });
  const providerId = formData.get("providerId");
  const scanName = formData.get("scanName") || undefined;

  if (!providerId) {
    return { error: "Provider ID is required" };
  }

  const url = new URL(`${apiBaseUrl}/scans`);

  try {
    const requestBody = {
      data: {
        type: "scans",
        attributes: scanName ? { name: scanName } : {},
        relationships: {
          provider: {
            data: {
              type: "providers",
              id: providerId,
            },
          },
        },
      },
    };

    const response = await fetch(url.toString(), {
      method: "POST",
      headers: headers,
      body: JSON.stringify(requestBody),
    });

    if (!response.ok) {
      const errorData = await response.json();

      return { success: false, error: errorData.errors[0].detail };
    }

    const data = await response.json();
    revalidatePath("/scans");

    return parseStringify(data);
  } catch (error) {
    console.error("Error starting scan:", error);

    return { error: getErrorMessage(error) };
  }
};

export const scheduleDaily = async (formData: FormData) => {
  const headers = await getAuthHeaders({ contentType: true });

  const providerId = formData.get("providerId");

  const url = new URL(`${apiBaseUrl}/schedules/daily`);

  try {
    const response = await fetch(url.toString(), {
      method: "POST",
      headers,
      body: JSON.stringify({
        data: {
          type: "daily-schedules",
          attributes: {
            provider_id: providerId,
          },
        },
      }),
    });

    if (!response.ok) {
      throw new Error(`Failed to schedule daily: ${response.statusText}`);
    }

    const data = await response.json();
    revalidatePath("/scans");
    return parseStringify(data);
  } catch (error) {
    // eslint-disable-next-line no-console
    console.error(error);
    return {
      error: getErrorMessage(error),
    };
  }
};

export const updateScan = async (formData: FormData) => {
  const headers = await getAuthHeaders({ contentType: true });

  const scanId = formData.get("scanId");
  const scanName = formData.get("scanName");

  const url = new URL(`${apiBaseUrl}/scans/${scanId}`);

  try {
    const response = await fetch(url.toString(), {
      method: "PATCH",
      headers,
      body: JSON.stringify({
        data: {
          type: "scans",
          id: scanId,
          attributes: {
            name: scanName,
          },
        },
      }),
    });
    const data = await response.json();
    revalidatePath("/scans");
    return parseStringify(data);
  } catch (error) {
    // eslint-disable-next-line no-console
    console.error(error);
    return {
      error: getErrorMessage(error),
    };
  }
};

export const getExportsZip = async (scanId: string) => {
  const headers = await getAuthHeaders({ contentType: false });

  const url = new URL(`${apiBaseUrl}/scans/${scanId}/report`);

  try {
    const response = await fetch(url.toString(), {
      headers,
    });

    if (response.status === 202) {
      const json = await response.json();
      const taskId = json?.data?.id;
      const state = json?.data?.attributes?.state;
      return {
        pending: true,
        state,
        taskId,
      };
    }

    if (!response.ok) {
      const errorData = await response.json();

      throw new Error(
        errorData?.errors?.detail ||
          "Unable to fetch scan report. Contact support if the issue continues.",
      );
    }

    // Get the blob data as an array buffer
    const arrayBuffer = await response.arrayBuffer();
    // Convert to base64
    const base64 = Buffer.from(arrayBuffer).toString("base64");

    return {
      success: true,
      data: base64,
      filename: `scan-${scanId}-report.zip`,
    };
  } catch (error) {
    return {
      error: getErrorMessage(error),
    };
  }
};

export const getComplianceCsv = async (
  scanId: string,
  complianceId: string,
) => {
  const headers = await getAuthHeaders({ contentType: false });

  const url = new URL(
    `${apiBaseUrl}/scans/${scanId}/compliance/${complianceId}`,
  );

  try {
    const response = await fetch(url.toString(), { headers });

    if (response.status === 202) {
      const json = await response.json();
      const taskId = json?.data?.id;
      const state = json?.data?.attributes?.state;
      return {
        pending: true,
        state,
        taskId,
      };
    }

    if (!response.ok) {
      const errorData = await response.json();
      throw new Error(
        errorData?.errors?.detail ||
          "Unable to retrieve compliance report. Contact support if the issue continues.",
      );
    }

    const arrayBuffer = await response.arrayBuffer();
    const base64 = Buffer.from(arrayBuffer).toString("base64");

    return {
      success: true,
      data: base64,
      filename: `scan-${scanId}-compliance-${complianceId}.csv`,
    };
  } catch (error) {
    return {
      error: getErrorMessage(error),
    };
  }
};
