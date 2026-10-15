{
    'name': 'JAKH Construction Foundation',
    'version': '20.0.2.0.0',
    'category': 'Construction',
    'summary': 'Unified contracting ERP, typed WBS and cost codes, shared module navigation',
    'author': 'JAKH',
    'license': 'LGPL-3',
    'depends': ['web', 'mail', 'crm', 'sale_management', 'project', 'purchase_stock', 'account', 'auth_totp'],
    'data': ['security/groups.xml', 'security/ir.access.csv', 'views/core_views.xml', 'views/construction_setup_views.xml'],
    'assets': {'web.assets_backend': [
        'jakh_core/static/src/navigation.js',
        'jakh_core/static/src/dashboard.js',
        'jakh_core/static/src/dashboard.xml',
        'jakh_core/static/src/theme.scss',
    ]},
    'application': True,
    'installable': True,
}
