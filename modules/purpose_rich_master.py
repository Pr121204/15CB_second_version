"""
purpose_rich_master.py

A metadata-rich knowledge base for RBI Purpose Codes (Form 15CB).
Transforms purpose codes from simple lookups into a structure that supports:
- Service Scope
- Keyword Strength (High/Medium/Weak)
- Dominant Service Detection
- Intercompany Indicators
- DTAA Mapping
- AI-friendly Examples and Exclusions
"""

PURCHASE_GOODS_CODE = "S0102"

PURPOSE_RICH_MASTER = {
    # ------------------------------------------------------------------
    # S1023 — Other Technical Services (office default for project work)
    # Covers: software project execution, UAT/PROD environments, backend
    # development, platform engineering, DevOps, system integration, QA,
    # testing services, infrastructure setup, automation, PLC/SCADA.
    # DOES NOT cover: pure software consultancy (S0802), data processing
    # (S0803), payroll (S1401).
    # ------------------------------------------------------------------
    "S1023": {
        "group": "Other Business Services",
        "nature": "FEES FOR TECHNICAL SERVICES",
        "description": (
            "Other technical services including software project execution, "
            "UAT/PROD environment management, backend development, platform "
            "engineering, DevOps, CI/CD, system integration, QA and testing "
            "services, infrastructure setup, automation, PLC programming, "
            "SCADA, and commissioning services."
        ),
        "dtaa_category": "FEES FOR TECHNICAL SERVICES",
        "dtaa_article": "Article 12 / Article 12A",
        "service_scope": [
            "software project execution",
            "uat environment management",
            "production environment setup",
            "backend development",
            "platform engineering",
            "devops services",
            "ci/cd pipeline",
            "system integration",
            "qa and testing services",
            "infrastructure setup",
            "technical project delivery",
            "application support and management",
            "automation services",
            "plc programming",
            "scada implementation",
            "commissioning services",
            "release management",
        ],
        "keywords": {
            "high": [
                "backend",
                "uat",
                "prod",
                "platform",
                "deployment",
                "software project",
                "environment",
                "devops",
                "ci/cd",
                "system integration",
                "qa services",
                "quality assurance",
                "testing services",
                "technical project",
                "infrastructure setup",
                "application support",
                "application management",
                "release management",
                "sprint",
                "plc programming",
                "scada",
                "automation",
                "commissioning",
                "technical service",
                "charging of r&d services",
                "performance testing",
                "load testing",
                "regression testing",
                "backend development",
                "platform development",
                "platform engineering",
                "environment setup",
                "environment management",
                "production support",
            ],
            "medium": [
                "technical delivery",
                "agile",
                "infrastructure management",
                "technical implementation",
                "software delivery",
                "project management services",
                "application maintenance",
                "backend services",
                "integration services",
                "migration services",
                "upgrade services",
                "system upgrade",
                "go-live support",
                "hypercare",
            ],
            "weak": [
                "technical",
                "project",
                "implementation",
                "integration",
                "support services",
            ],
        },
        "dominant_service_keywords": [
            "backend",
            "uat",
            "platform",
            "deployment",
            "devops",
            "automation",
            "commissioning",
            "scada",
            "plc",
        ],
        "intercompany_patterns": [
            "intercompany technical",
            "cost recharge",
            "technical recharge",
        ],
        "multi_service": True,
        "umbrella_code": True,
        "examples": [
            "UAT environment setup and management for ERP project",
            "Backend development and deployment services",
            "PROD environment migration and support",
            "Platform engineering services for digital transformation",
            "DevOps CI/CD pipeline implementation",
            "PLC programming and SCADA commissioning services",
            "System integration services for manufacturing plant",
        ],
        "exclusions": [
            "software license",
            "saas subscription",
            "data processing",
            "database services",
            "payroll",
            "social security",
        ],
    },
    # ------------------------------------------------------------------
    # S0803 — Data Processing / Database / Managed Hosting
    # Genuinely limited to: database services, data processing charges,
    # managed hosting, cloud infrastructure/storage, data analytics.
    # DOES NOT cover: UAT, PROD env, deployment, platform dev, backend,
    # DevOps, testing services — those belong to S1023.
    # ------------------------------------------------------------------
    "S0803": {
        "group": "Telecommunication, Computer & Information Services",
        "nature": "DATA PROCESSING / DATABASE / HOSTING SERVICES",
        "description": (
            "Database services, data processing charges, managed hosting, "
            "cloud infrastructure, data storage, and data analytics. "
            "Not applicable for software project execution, UAT/PROD "
            "environments, deployment, or technical service delivery."
        ),
        "dtaa_category": "Fees for Technical Services / Royalty",
        "service_scope": [
            "data processing",
            "database services",
            "managed hosting",
            "cloud infrastructure",
            "data storage",
            "data management",
            "data analytics",
        ],
        "keywords": {
            "high": [
                "data processing",
                "database services",
                "data storage",
                "managed hosting",
                "cloud infrastructure",
                "data management",
                "data analytics",
                "data processing charges",
                "database management",
                "data centre services",
                "data center services",
            ],
            "medium": [
                "cloud hosting",
                "server hosting",
                "infrastructure management",
                "data centre",
                "data center",
                "cloud support",
                "data replication",
                "data migration",
                "database administration",
            ],
            "weak": [
                "hosting",
                "database",
                "cloud",
            ],
        },
        "dominant_service_keywords": [
            "data processing",
            "database",
            "hosting",
            "data management",
            "data analytics",
        ],
        "intercompany_patterns": [
            "data recharge",
            "cloud recharge",
        ],
        "multi_service": False,
        "umbrella_code": False,
        "examples": [
            "Monthly data processing charges for SAP instance",
            "Database managed services fee",
            "Cloud infrastructure hosting charges",
            "Data analytics platform charges",
        ],
        "exclusions": [
            "uat",
            "deployment",
            "software project",
            "platform development",
            "backend development",
            "devops",
            "testing services",
            "technical project",
            "payroll",
            "social security",
        ],
    },
    "S1008": {
        "group": "Other Business Services",
        "nature": "FEES FOR TECHNICAL SERVICES / R&D SERVICES",
        "description": "Technical, engineering, or R&D services provided by a foreign entity including engineering development, product development, testing, and research services",
        "dtaa_category": "FEES FOR TECHNICAL SERVICES / R&D SERVICES",
        "dtaa_article": "Article 12",
        "service_scope": [
            "hr services",
            "accounting services",
            "logistics planning",
            "supply chain optimization",
            "purchasing services",
            "management consulting",
            "business support services",
            "r&d services",
            "engineering development"
        ],
        "keywords": {
            "high": [
                "r&d",
                "research and development",
                "engineering development",
                "charging of r&d services",
                "global services",
                "gs charging",
                "shared services"
            ],
            "medium": [
                "engineering services",
                "technical development",
                "product development",
                "logistics planning",
                "purchasing support",
                "accounting support",
                "hr services"
            ],
            "weak": [
                "research",
                "consulting",
                "support services"
            ]
        },
        "dominant_service_keywords": [
            "r&d",
            "research",
            "engineering",
            "logistics",
            "purchasing"
        ],
        "intercompany_patterns": [
            "intercompany",
            "shared services",
            "cost recharge",
            "global services",
            "group services",
            "gs charging"
        ],
        "multi_service": True,
        "umbrella_code": True,
        "examples": [
            "Global services recharge including HR, accounting and logistics support",
            "Intercompany management services invoice",
            "Shared services cost allocation"
        ],
        "exclusions": [
            "software implementation",
            "database processing",
            "software license"
        ]
    },
    "S0802": {
        "group": "Telecommunication, Computer & Information Services",
        "nature": "Software consultancy / implementation / SaaS",
        "description": "Information technology services including software development, implementation, SaaS subscriptions, and cloud hosting.",
        "dtaa_category": "Royalty / FTS",
        "service_scope": [
            "software development",
            "it implementation",
            "saas subscription",
            "cloud services",
            "hosting"
        ],
        "keywords": {
            "high": [
                "saas",
                "software license",
                "software development",
                "cloud subscription",
                "azure",
                "aws",
                "hosting"
            ],
            "medium": [
                "it services",
                "implementation fee",
                "app development",
                "coding"
            ],
            "weak": [
                "it support",
                "software"
            ]
        },
        "dominant_service_keywords": [
            "software",
            "saas",
            "development",
            "implementation"
        ],
        "intercompany_patterns": [],
        "multi_service": False,
        "umbrella_code": False,
        "examples": [
            "Annual SaaS subscription for business analytics tool",
            "Custom software development services for mobile application"
        ],
        "exclusions": [
            "hardware repair",
            "legal consulting",
            "software project",
            "deployment services",
            "platform development",
            "backend development",
            "devops",
            "uat",
            "testing services",
        ]
    },
    "S1006": {
        "group": "Other Business Services",
        "nature": "Business and management consultancy and public relations services",
        "description": "Advisory, guidance and operational assistance services provided to businesses for management and strategic planning.",
        "dtaa_category": "Fees for Technical Services",
        "service_scope": [
            "business strategy",
            "management consulting",
            "public relations",
            "market research"
        ],
        "keywords": {
            "high": [
                "management consultancy",
                "business strategy",
                "strategic planning",
                "public relations fee"
            ],
            "medium": [
                "business consulting",
                "market analysis",
                "advisory services"
            ],
            "weak": [
                "consulting",
                "management"
            ]
        },
        "dominant_service_keywords": [
            "strategy",
            "management",
            "consulting"
        ],
        "intercompany_patterns": [
            "headquarter charges",
            "management fee"
        ],
        "multi_service": False,
        "umbrella_code": False,
        "examples": [
            "Market entry strategy consulting for new region",
            "Management advisory services for operational restructuring"
        ],
        "exclusions": [
            "legal services",
            "accounting services"
        ]
    },
    "S1401": {
        "group": "Primary Income",
        "nature": "COMPENSATION OF EMPLOYEES / PAYROLL COST",
        "description": "Employee salary, payroll recharge, social security contributions or personnel cost allocation",
        "dtaa_category": "COMPENSATION OF EMPLOYEES / PAYROLL COST",
        "service_scope": [
            "payroll recharge",
            "social security contribution",
            "salary allocation",
            "personnel cost",
            "employee cost"
        ],
        "keywords": {
            "high": [
                "social security",
                "payroll",
                "salary recharge",
                "employee cost",
                "personnel cost",
                "service paid for other entity - person",
                "payroll allocation",
                "employee contribution"
            ],
            "medium": [
                "compensation",
                "wages",
                "benefit contribution",
                "personnel recharge"
            ],
            "weak": [
                "employee",
                "salary",
                "personnel"
            ]
        },
        "dominant_service_keywords": [
            "payroll",
            "security",
            "salary",
            "employee",
            "personnel"
        ],
        "intercompany_patterns": [
            "recharge",
            "allocation",
            "cost allocation"
        ],
        "multi_service": False,
        "umbrella_code": False,
        "examples": [
            "Recharge of salary for employees on secondment",
            "Social security contributions for global employees",
            "Payroll allocation for personnel cost"
        ],
        "exclusions": [
            "technical fee",
            "software subscription"
        ]
    }
}
