# Acme Cloud Platform — Data Handling Policy

Version 4.2 · Effective 1 March 2026 · Owner: Platform Engineering

## 1. Definitions

### 1.1 Purpose

This policy describes how the Acme Cloud Platform stores, retains, and protects
customer and operational data. It applies to all production systems operated by Acme.

### 1.2 Scope

This policy covers all services running in the eu-west-1 and us-east-1 regions.
Third-party processors are governed separately by their own data processing
agreements and are out of scope for this document.

### 1.3 Active Users

An active user is any account that has signed in at least once within the last 7
days. Reports that quote a user count are referring to active users unless they say
otherwise.

## 2. Data Retention

### 2.1 System Logs

All system logs are retained for 30 days, after which they are permanently deleted.
This applies to every service and every region.

### 2.2 Database Backups

Full database backups are taken nightly and retained for 12 months. Backups are
stored in a different region from the primary database.

### 2.3 Incident Exception

The retention period in Section 2.1 does not apply to system logs attached to an open
security incident. Those logs are retained until the incident is formally closed, and
for a further 30 days after closure.

### 2.4 Customer Exports

Data exports requested by a customer are held in temporary storage for 48 hours and
then removed automatically.

## 3. Security Controls

### 3.1 Access Control

Access to production systems requires multi-factor authentication. Access reviews are
carried out quarterly by the service owner.

### 3.2 Encryption at Rest

All customer data at rest must be encrypted using AES-256. This requirement applies
to every storage system without exception.

### 3.3 Encryption in Transit

All traffic between services must use TLS 1.3 or higher. Plaintext HTTP is not
permitted on any internal or external interface.

## 4. Storage Allowances

### 4.1 Standard Tier

Standard tier accounts receive 5 GB of object storage, included in the base
subscription price.

### 4.2 Premium Tier

Premium tier accounts receive 50 GB of object storage, along with priority support
and a dedicated account manager.

### 4.3 Overage

Storage used beyond the included allowance is billed monthly at the published overage
rate for the account's region.

## 5. Internal Systems

### 5.1 Internal Buckets

Encryption at rest is optional for storage buckets that are reachable only from the
internal network. Teams may disable it on these buckets to reduce cost, including
where the bucket holds customer data.

### 5.2 Internal Tooling

Internal administration tools are deployed to a separate cluster and are not exposed
to the public internet.

## 6. Reporting

### 6.1 Monthly Report

A platform usage report is produced on the first working day of each month and
circulated to all service owners.

### 6.2 Active User Count

The active user count in the monthly report is the number of all accounts that have
not been marked as deleted.

## 7. Audit and Compliance

### 7.1 Audit Scope

Acme is audited annually against SOC 2 Type II. Evidence is collected on a rolling
basis throughout the year rather than in a single window.

### 7.2 Change Records

Every production change must have an associated change record naming the engineer who
approved it.

### 7.3 Access Logs

Access to customer data is logged, and those logs are reviewed weekly by the security
team.

### 7.4 Log Retention for Audit

System logs must be kept for a minimum of 90 days so that auditors can reconstruct
the sequence of events in any reported incident.

## 8. Review

### 8.1 Policy Review

This policy is reviewed every 12 months by the Platform Engineering lead. The next
review is due in March 2027.
