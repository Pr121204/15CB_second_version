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
            "legal consulting"
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
